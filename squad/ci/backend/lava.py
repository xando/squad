import json
import traceback
import yaml
import xmlrpc
import zmq

from zmq.utils.strtypes import u

from xmlrpc import client as xmlrpclib
from urllib.parse import urlsplit


from squad.ci.models import TestJob
from squad.ci.tasks import fetch
from squad.ci.exceptions import SubmissionIssue, TemporarySubmissionIssue
from squad.ci.backend.null import Backend as BaseBackend


description = "LAVA"


class Backend(BaseBackend):

    # ------------------------------------------------------------------------
    # API implementation
    # ------------------------------------------------------------------------
    def submit(self, test_job):
        try:
            job_id = self.__submit__(test_job.definition)
            return job_id
        except xmlrpc.client.ProtocolError as error:
            raise TemporarySubmissionIssue(str(error))
        except xmlrpc.client.Fault as fault:
            if fault.faultCode // 100 == 5:
                # assume HTTP errors 5xx are temporary issues
                raise TemporarySubmissionIssue(str(fault))
            else:
                raise SubmissionIssue(str(fault))

    def fetch(self, test_job):
        data = self.__get_job_details__(test_job.job_id)

        if data['status'] in self.complete_statuses:
            yamldata = self.__get_testjob_results_yaml__(test_job.job_id)
            data['results'] = yaml.load(yamldata)

            # fetch logs
            logs = ""
            try:
                logs = self.__get_job_logs__(test_job.job_id)
            except:
                self.log_warn("Logs for job %s are not available" % test_job.job_id)

            return self.__parse_results__(data, test_job) + (logs,)

    def listen(self):
        listener_url = self.get_listener_url()

        self.log_debug("connecting to %s" % listener_url)

        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "")
        try:
            # requires PyZMQ to be built against ZeroMQ 4.2+
            self.socket.setsockopt(zmq.HEARTBEAT_IVL, 1000)  # 1 s
            self.socket.setsockopt(zmq.HEARTBEAT_TIMEOUT, 10000)  # 10 s
        except AttributeError:
            self.log_warn('PyZMQ has no support for heartbeat (requires ZeroMQ library 4.2+), connection may be unstable')
            pass

        self.socket.connect(listener_url)

        self.log_debug("connected to %s" % listener_url)

        while True:
            try:
                message = self.socket.recv_multipart()
                self.log_debug("message received: %r" % message)
                (topic, uuid, dt, username, data) = (u(m) for m in message[:])
                data = json.loads(data)
                lava_id = data['job']
                if 'sub_id' in data.keys():
                    lava_id = data['sub_id']
                lava_status = data['status']
                if lava_status in self.complete_statuses:
                    db_test_job_list = self.data.test_jobs.filter(
                        submitted=True,
                        fetched=False,
                        job_id=lava_id)
                    if db_test_job_list.exists() and \
                            len(db_test_job_list) == 1:
                        job = db_test_job_list[0]
                        self.log_info("scheduling fetch for job %s" % job.job_id)
                        fetch.delay(job.id)
            except Exception as e:
                self.log_error(str(e) + "\n" + traceback.format_exc())

    def job_url(self, test_job):
        url = urlsplit(self.data.url)
        joburl = '%s://%s/scheduler/job/%s' % (
            url.scheme,
            url.netloc,
            test_job.job_id
        )
        return joburl

    # ------------------------------------------------------------------------
    # implementation details
    # ------------------------------------------------------------------------
    def __init__(self, data):
        super(Backend, self).__init__(data)
        self.complete_statuses = ['Complete', 'Incomplete', 'Canceled']
        self.__proxy__ = None

    @property
    def proxy(self):
        if self.__proxy__ is None:
            url = urlsplit(self.data.url)
            endpoint = '%s://%s:%s@%s%s' % (
                url.scheme,
                self.data.username,
                self.data.token,
                url.netloc,
                url.path
            )
            self.__proxy__ = xmlrpclib.ServerProxy(endpoint)
        return self.__proxy__

    def get_listener_url(self):
        url = urlsplit(self.data.url)
        hostname = url.netloc

        socket = self.__get_publisher_event_socket__()
        socket_url = urlsplit(socket)
        port = socket_url.port
        if socket_url.hostname != '*':
            hostname = socket_url.hostname
        scheme = socket_url.scheme
        return '%s://%s:%s' % (scheme, hostname, port)

    def resubmit(self, test_job):
        if test_job.job_id is not None:
            new_job_id = self.__resubmit__(test_job.job_id)
            new_test_job = TestJob(
                backend=self.data,
                testrun=test_job.testrun,
                definition=test_job.definition,
                target=test_job.target,
                build=test_job.build,
                environment=test_job.environment,
                submitted=True,
                job_id=new_job_id
            )
            new_test_job.save()

    def __resubmit__(self, job_id):
        return self.proxy.scheduler.resubmit_job(job_id)

    def __submit__(self, definition):
        return self.proxy.scheduler.submit_job(definition)

    def __get_job_details__(self, job_id):
        return self.proxy.scheduler.job_details(job_id)

    def __get_job_logs__(self, job_id):
        return self.proxy.scheduler.job_output(job_id).data.decode('utf-8')

    def __get_testjob_results_yaml__(self, job_id):
        return self.proxy.results.get_testjob_results_yaml(job_id)

    def __get_publisher_event_socket__(self):
        return self.proxy.scheduler.get_publisher_event_socket()

    def __parse_results__(self, data, test_job):
        if data['is_pipeline'] is False:
            # in case of v1 job, return empty data
            return (data['status'], {}, {}, {})
        definition = yaml.load(data['definition'])
        if data['multinode_definition']:
            definition = yaml.load(data['multinode_definition'])
        job_metadata = definition['metadata']
        results = {}
        metrics = {}
        completed = True
        for result in data['results']:
            if result['suite'] != 'lava':
                suite = result['suite'].split("_", 1)[1]
                res_name = "%s/%s" % (suite, result['name'])
                # YAML from LAVA has all values serialized to strings
                if result['measurement'] == 'None':
                    res_value = result['result']
                    results.update({res_name: res_value})
                else:
                    res_value = result['measurement']
                    metrics.update({res_name: float(res_value)})
            else:
                if result['name'] == 'job' and result['result'] == 'fail':
                    metadata = result['metadata']
                    # detect jobs failed because of infrastructure issues
                    if metadata['error_type'] in ['Infrastructure', 'Lava', 'Job']:
                        completed = False

        return (data['status'], completed, job_metadata, results, metrics)
