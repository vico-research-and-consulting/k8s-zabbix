import logging
import json
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pprint import pformat

from k8sobjects.k8sobject import K8sObject
from k8sobjects.k8sresourcemanager import K8sResourceManager
from k8sobjects.pvc import get_pvc_volumes_for_all_nodes
from k8sobjects.container import get_container_zabbix_metrics
from kubernetes import client, watch
from kubernetes import config as kube_config
from kubernetes import watch
from kubernetes.client import (ApiClient, AppsV1Api, CoreV1Api,
                               ApiextensionsV1Api)
from pyzabbix import ZabbixMetric, ZabbixResponse, ZabbixSender

from base.config import ClusterAccessConfigType, Configuration
from base.timed_threads import TimedThread
from base.watcher_thread import WatcherThread

from .web_api import WebApi

exit_flag = threading.Event()


@dataclass
class DryResult:
    failed: int = 0
    processed: int = 0


def get_data_timeout_datetime() -> datetime:
    return datetime.now() - timedelta(minutes=1)


def get_discovery_timeout_datetime() -> datetime:
    return datetime.now() - timedelta(hours=1)


class KubernetesApi:
    __shared_state = dict(core_v1=None, apps_v1=None, extensions_v1=None)

    def __init__(self, api_client: ApiClient):
        self.__dict__ = self.__shared_state
        if not getattr(self, "core_v1", None):
            self.core_v1 = client.CoreV1Api(api_client)
        if not getattr(self, "apps_v1", None):
            self.apps_v1 = client.AppsV1Api(api_client)
        if not getattr(self, 'extensions_v1', None):
            self.extensions_v1 = client.ApiextensionsV1Api(api_client)


class CheckKubernetesDaemon:
    data: dict[str, K8sResourceManager] = {}
    discovery_sent: dict[str, datetime] = {}
    thread_lock = threading.Lock()
    data_refreshed: dict[str, datetime] = {}

    def __init__(self, config: Configuration,
                 resources: list[str],
                 discovery_interval: int,
                 data_resend_interval: int,
                 data_refresh_interval: int,
                 ):
        self.manage_threads: list[TimedThread | WatcherThread] = []
        self.config: Configuration = config
        self.logger = logging.getLogger("k8s-zabbix")
        self.discovery_interval = int(discovery_interval)
        self.data_resend_interval = int(data_resend_interval)
        self.data_refresh_interval = int(data_refresh_interval)

        self.api_zabbix_interval = 60
        self.rate_limit_seconds = 30

        if config.k8s_config_type is ClusterAccessConfigType.INCLUSTER:
            kube_config.load_incluster_config()
            self.api_client = client.ApiClient()
        elif config.k8s_config_type is ClusterAccessConfigType.KUBECONFIG:
            kube_config.load_kube_config()
            self.api_client = kube_config.new_client_from_config()
        elif config.k8s_config_type is ClusterAccessConfigType.TOKEN:
            self.api_configuration = client.Configuration()
            self.api_configuration.host = config.k8s_api_host
            self.api_configuration.verify_ssl = config.verify_ssl
            self.api_configuration.api_key = {"authorization": "Bearer " + config.k8s_api_token}
            self.api_client = client.ApiClient(self.api_configuration)
        else:
            self.logger.fatal(f"k8s_config_type = {config.k8s_config_type} is not implemented")
            sys.exit(1)

        self.logger.info(f"Initialized cluster access for {config.k8s_config_type}")
        # K8S API
        self.debug_k8s_events = False
        self.apis = {
            'core_v1': KubernetesApi(self.api_client).core_v1,
            'apps_v1': KubernetesApi(self.api_client).apps_v1,
            'extensions_v1': KubernetesApi(self.api_client).extensions_v1
        }

        self.zabbix_sender = ZabbixSender(zabbix_server=config.zabbix_server)
        self.zabbix_resources = CheckKubernetesDaemon.exclude_resources(resources,
                                                                        config.zabbix_resources_exclude)
        self.zabbix_host = config.zabbix_host
        self.zabbix_debug = config.zabbix_debug
        self.zabbix_single_debug = config.zabbix_single_debug
        self.zabbix_dry_run = config.zabbix_dry_run

        self.web_api = None
        self.web_api_enable = config.web_api_enable
        self.web_api_resources = CheckKubernetesDaemon.exclude_resources(resources,
                                                                         config.web_api_resources_exclude)

        self.web_api_host = config.web_api_host
        self.web_api_token = config.web_api_token
        self.web_api_cluster = config.web_api_cluster
        self.web_api_verify_ssl = config.web_api_verify_ssl

        self.resources = CheckKubernetesDaemon.exclude_resources(resources, config.resources_exclude)

        self.logger.info(f"Init K8S-ZABBIX Watcher for resources: {','.join(self.resources)}")
        self.logger.info(f"Zabbix Host: {self.zabbix_host} / Zabbix Proxy or Server: {config.zabbix_server}")
        if self.web_api_enable:
            self.logger.info(f"WEB Api Host {self.web_api_host} with resources {','.join(self.web_api_resources)}")

    @staticmethod
    def exclude_resources(available_types: list[str], excluded_types: list[str]) -> list[str]:
        result = []
        for k8s_type_available in available_types:
            if k8s_type_available not in excluded_types:
                result.append(k8s_type_available)
        return result

    def handler(self, signum: int, *args: str) -> None:
        if signum in [signal.SIGTERM]:
            self.logger.info("Signal handler called with signal %s... stopping (max %s seconds)" % (signum, 3))
            exit_flag.set()
            for thread in self.manage_threads:
                thread.join(timeout=3)
            self.logger.info("All threads exited... exit check_kubernetesd")
            sys.exit(0)
        elif signum in [signal.SIGUSR1]:
            self.logger.info('=== Listing count of data hold in CheckKubernetesDaemon.data ===')
            with self.thread_lock:
                for r, d in self.data.items():
                    for obj_name, obj_d in d.objects.items():
                        self.logger.info(
                            f"resource={r}, [{obj_name}], last_sent_zabbix={obj_d.last_sent_zabbix}, " + f"last_sent_web={obj_d.last_sent_web}"
                        )
                for resource_discovered, resource_discovered_time in self.discovery_sent.items():
                    self.logger.info(
                        f"resource={resource_discovered}, last_discovery_sent={resource_discovered_time}")
        elif signum in [signal.SIGUSR2]:
            self.logger.info('=== Listing all data hold in CheckKubernetesDaemon.data ===')
            with self.thread_lock:
                for r, d in self.data.items():
                    for obj_name, obj_d in d.objects.items():
                        data_print = pformat(obj_d.data, indent=2)
                        self.logger.info(f"resource={r}, object_name={obj_name}, object_data={data_print}")

    def run(self) -> None:
        self.start_data_threads()
        self.start_api_info_threads()
        self.start_loop_send_discovery_threads()
        self.start_resend_threads()

    def excepthook(self, args):
        self.logger.exception(f"Thread '{self.resources}' failed: {args.exc_value}")

    def start_data_threads(self) -> None:
        thread: WatcherThread | TimedThread
        threading.excepthook = self.excepthook
        for resource in self.resources:
            with self.thread_lock:
                self.data.setdefault(resource, K8sResourceManager(resource,
                                                                  apis=self.apis,
                                                                  zabbix_host=self.zabbix_host,
                                                                  config=self.config))
                if resource == "pods":
                    # additional containers coming from pods
                    self.data.setdefault("containers", K8sResourceManager("containers",
                                                                          config=self.config))

            if resource in ['containers', 'services']:
                thread = TimedThread(resource, self.data_resend_interval, exit_flag,
                                     daemon_object=self, daemon_method='report_global_data_zabbix',
                                     delay_first_run=True,
                                     delay_first_run_seconds=self.discovery_interval + 5)
                self.manage_threads.append(thread)
                thread.start()
            elif resource in ['components', 'pvcs']:
                thread = TimedThread(resource, self.data_resend_interval, exit_flag,
                                     daemon_object=self, daemon_method='watch_data')
                self.manage_threads.append(thread)
                thread.start()
            else:
                thread = WatcherThread(resource, exit_flag,
                                       daemon_object=self, daemon_method='watch_data')
                self.manage_threads.append(thread)
                thread.start()

    def start_api_info_threads(self) -> None:
        if 'nodes' not in self.resources:
            # only send api heartbeat once
            return

        thread = TimedThread('api_heartbeat', self.api_zabbix_interval, exit_flag,
                             daemon_object=self, daemon_method='send_heartbeat_info')
        self.manage_threads.append(thread)
        thread.start()

    def start_loop_send_discovery_threads(self) -> None:
        for resource in self.resources:
            if resource == 'containers':
                # skip containers as discovery is done by pods
                continue

            send_discovery_thread = TimedThread(resource, self.discovery_interval, exit_flag,
                                                daemon_object=self, daemon_method='update_discovery',
                                                delay_first_run=True,
                                                delay_first_run_seconds=self.config.discovery_interval_delay)
            self.manage_threads.append(send_discovery_thread)
            send_discovery_thread.start()

    def start_resend_threads(self) -> None:
        for resource in self.resources:
            resend_thread = TimedThread(resource, self.data_resend_interval, exit_flag,
                                        daemon_object=self, daemon_method='resend_data',
                                        delay_first_run=True,
                                        delay_first_run_seconds=self.config.data_resend_interval_delay,
                                        )
            self.manage_threads.append(resend_thread)
            resend_thread.start()

    def get_api_for_resource(self, resource: str) -> CoreV1Api | AppsV1Api | ApiextensionsV1Api:
        if resource in ['nodes', 'components', 'secrets', 'pods', 'services', 'pvcs']:
            api = self.core_v1
        elif resource in ['deployments', 'daemonsets', 'statefulsets']:
            api = self.apps_v1
        elif resource in ['ingresses']:
            api = self.extensions_v1
        else:
            raise AttributeError('No valid resource found: %s' % resource)
        return api

    def get_web_api(self) -> WebApi:
        if not hasattr(self, '_web_api'):
            self._web_api = WebApi(self.web_api_host, self.web_api_token, verify_ssl=self.web_api_verify_ssl)
        return self._web_api

    def watch_data(self, resource: str) -> None:
        api = self.data[resource].api
        stream_named_arguments = {"timeout_seconds": self.config.k8s_api_stream_timeout_seconds}
        request_named_arguments = {"_request_timeout": self.config.k8s_api_request_timeout_seconds}
        self.logger.info(
            "Watching for resource >>>%s<<< with a stream duration of %ss or request_timeout of %ss" % (
                resource,
                self.config.k8s_api_stream_timeout_seconds,
                self.config.k8s_api_request_timeout_seconds)
        )
        while True:
            w = watch.Watch()
            if resource == 'nodes':
                for obj in w.stream(api.list_node, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'deployments':
                for obj in w.stream(api.list_deployment_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'daemonsets':
                for obj in w.stream(api.list_daemon_set_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'statefulsets':
                for obj in w.stream(api.list_stateful_set_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == "components":
                # The api does not support watching on component status
                with self.thread_lock:
                    for obj in api.list_component_status(watch=False, **request_named_arguments).to_dict().get('items'):
                        self.data[resource].add_obj_from_data(obj)
                time.sleep(self.data_resend_interval)
            elif resource == 'pvcs':
                pvc_volumes = get_pvc_volumes_for_all_nodes(api=api,
                                                            timeout=self.config.k8s_api_request_timeout_seconds,
                                                            namespace_exclude_re=self.config.namespace_exclude_re,
                                                            resource_manager=self.data[resource])
                with self.thread_lock:
                    for obj in pvc_volumes:
                        self.data[resource].add_obj(obj)
                time.sleep(self.data_resend_interval)
            elif resource == 'ingresses':
                for obj in w.stream(api.list_ingress_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'tls':
                for obj in w.stream(api.list_secret_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'pods':
                for obj in w.stream(api.list_pod_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            elif resource == 'services':
                for obj in w.stream(api.list_service_for_all_namespaces, **stream_named_arguments):
                    self.watch_event_handler(resource, obj)
            else:
                self.logger.error("No watch handling for resource %s" % resource)
                time.sleep(60)
            self.logger.debug("Watch/fetch completed for resource >>>%s<<<, restarting" % resource)

    def watch_event_handler(self, resource: str, event: dict) -> None:

        obj = event['object'].to_dict()
        event_type = event['type']
        name = obj['metadata']['name']
        namespace = str(obj['metadata']['namespace'])

        if self.config.namespace_exclude_re and re.match(self.config.namespace_exclude_re, namespace):
            self.logger.debug(f"skip namespace {namespace}")
            return

        if self.debug_k8s_events:
            self.logger.info(f"{event_type} [{resource}]: {namespace}/{name} : >>>{pformat(obj, indent=2)}<<<")
        else:
            self.logger.debug(f"{event_type} [{resource}]: {namespace}/{name}")

        with self.thread_lock:
            if not self.data[resource].resource_class:
                self.logger.error('Could not add watch_event_handler! No resource_class for "%s"' % resource)
                return

        if event_type.lower() in ['added', 'modified']:
            with self.thread_lock:
                resourced_obj = self.data[resource].add_obj_from_data(obj)
                if resourced_obj and (resourced_obj.is_dirty_zabbix or resourced_obj.is_dirty_web):
                    self.send_object(resource, resourced_obj, event_type,
                                     send_zabbix_data=resourced_obj.is_dirty_zabbix,
                                     send_web=resourced_obj.is_dirty_web)
        elif event_type.lower() == 'deleted':
            with self.thread_lock:
                resourced_obj = self.data[resource].del_obj(obj)
                if resourced_obj:
                    self.delete_object(resource, resourced_obj)
        else:
            self.logger.info('event type "%s" not implemented' % event_type)
        self.logger.debug(f'watch_event_handler[{resource}] finished')

    def report_global_data_zabbix(self, resource: str) -> None:
        """ aggregate and report information for some speciality in resources """
        if resource not in self.discovery_sent:
            self.logger.info('skipping report_global_data_zabbix for %s, discovery not send yet!' % resource)
            return

        data_to_send = list()

        if resource == "services":
            num_services = 0
            num_ingress_services = 0
            with self.thread_lock:
                for obj_uid, resourced_obj in self.data[resource].objects.items():
                    num_services += 1
                    if resourced_obj.resource_data["is_ingress"]:
                        num_ingress_services += 1

            data_to_send.append(
                ZabbixMetric(self.zabbix_host, 'check_kubernetes[get,services,num_services]',
                             str(num_services)))
            data_to_send.append(
                ZabbixMetric(self.zabbix_host, 'check_kubernetes[get,services,num_ingress_services]',
                             str(num_ingress_services)))
            self.send_data_to_zabbix(resource, None, data_to_send)

        elif resource == "containers":
            # aggregate pod data to containers for each namespace
            with self.thread_lock:
                containers = dict()
                for obj_uid, resourced_obj in self.data["pods"].objects.items():
                    ns = resourced_obj.name_space
                    if ns not in containers:
                        containers[ns] = dict()

                    pod_data = resourced_obj.resource_data
                    pod_base_name = resourced_obj.base_name
                    try:
                        container_status = json.loads(pod_data["container_status"])
                    except Exception as e:
                        self.logger.error(e)
                        continue

                    # aggregate container information
                    for container_name, container_data in container_status.items():
                        containers[ns].setdefault(pod_base_name, dict())
                        if container_name not in containers[ns][pod_base_name]:
                            containers[ns][pod_base_name].setdefault(container_name, container_data)
                        else:
                            for k, v in containers[ns][pod_base_name][container_name].items():
                                if isinstance(v, int):
                                    containers[ns][pod_base_name][container_name][k] += container_data[k]
                                elif k == "status" and container_data[k].startswith("ERROR"):
                                    containers[ns][pod_base_name][container_name][k] = container_data[k]
                        # self.logger.debug("%s %s %s" % (resourced_obj.name, container_name, containers[ns][pod_base_name][container_name]))
                for ns, d1 in containers.items():
                    for pod_base_name, d2 in d1.items():
                        for container_name, container_data in d2.items():
                            data_to_send += get_container_zabbix_metrics(
                                self.zabbix_host, ns, pod_base_name, container_name, container_data
                            )

                self.send_data_to_zabbix(resource, None, data_to_send)

    def resend_data(self, resource: str) -> None:
        if resource == 'containers':
            return

        with self.thread_lock:
            try:
                metrics = list()
                if resource not in self.data or len(self.data[resource].objects) == 0:
                    self.logger.warning("no resource data available for %s , stop delivery" % resource)
                    return

                # Zabbix
                for obj_uid, obj in self.data[resource].objects.items():
                    zabbix_send = False
                    if resource in self.discovery_sent and obj.added > self.discovery_sent[resource]:
                        self.logger.info(
                            f'skipping resend of {obj}, resource {resource} discovery_sent "{self.discovery_sent[resource].isoformat()}"'
                            f' is older than {obj.added.isoformat()}')
                    elif obj.last_sent_zabbix < (datetime.now() - timedelta(seconds=self.data_resend_interval)):
                        self.logger.debug(
                            "resend zabbix : %s  - %s/%s data because its outdated"
                            % (resource, obj.name_space, obj.name)
                        )
                        zabbix_send = True
                    if zabbix_send:
                        metrics += obj.get_zabbix_metrics()
                        obj.last_sent_zabbix = datetime.now()
                        obj.is_dirty_zabbix = False
                if len(metrics) > 0:
                    if resource not in self.discovery_sent:
                        self.logger.debug(
                            "skipping resend_data zabbix , discovery for %s - %s/%s not sent yet!"
                            % (resource, obj.name_space, obj.name)
                        )
                    else:
                        self.send_data_to_zabbix(resource, metrics=metrics)

                # Web
                for obj_uid, obj in self.data[resource].objects.items():
                    if obj.is_dirty_web:
                        if obj.is_unsubmitted_web():
                            self.send_to_web_api(resource, obj, "ADDED")
                        else:
                            self.send_to_web_api(resource, obj, "MODIFIED")
                    else:
                        if obj.is_unsubmitted_web():
                            self.send_to_web_api(resource, obj, "ADDED")
                        elif obj.last_sent_web < (datetime.now() - timedelta(seconds=self.data_resend_interval)):
                            self.send_to_web_api(resource, obj, "MODIFIED")
                            self.logger.debug("resend web : %s/%s data because its outdated" % (resource, obj.name))
                    obj.last_sent_web = datetime.now()
                    obj.is_dirty_web = False
            except RuntimeError as e:
                self.logger.warning(str(e))

    def delete_object(self, resource_type: str, resourced_obj: K8sObject) -> None:
        self.send_to_web_api(resource_type, resourced_obj, "deleted")

    def update_discovery(self, resource: str) -> None:
        """ Update elements on hold and send to zabbix """
        resource_obj = self.data[resource].resource_meta
        with (self.thread_lock):
            self.logger.debug(f"update_discovery[{resource}]: got thread_lock")
            if resource in self.data_refreshed \
                    and self.data_refreshed[resource] < (datetime.now() - timedelta(seconds=self.data_refresh_interval)) \
                    or resource not in self.data_refreshed:
                obj_uid_list, obj_data_list = resource_obj.get_uid_list_and_data()
                obj_uid_list_len = len(obj_uid_list)
                self.logger.info(f"refreshing [{resource}] uid_list + data and check for orphans: {obj_uid_list_len}")
                if resource in self.data_refreshed:
                    self.logger.info(f"last refresh: {self.data_refreshed[resource]}")

                # copy dict to delete in it
                for obj_uid in self.data[resource].objects.copy():
                    if obj_uid not in obj_uid_list:
                        self.logger.info(f"NOT finding [{resource}]{obj_uid} anymore -> removing")
                        self.data[resource].del_obj(obj_uid)
                    else:
                        # update obj information
                        self.data[resource].add_obj(obj_data_list[obj_uid])

                self.data_refreshed[resource] = datetime.now()
            self.send_zabbix_discovery(resource)

    def send_zabbix_discovery(self, resource: str) -> None:
        # aggregate data and send to zabbix
        next_run = datetime.now() + timedelta(seconds=self.discovery_interval)
        self.logger.info(f"send_zabbix_discovery: {resource}, next run: {next_run.isoformat()}")

        if resource not in self.data:
            self.logger.warning('send_zabbix_discovery: resource "%s" not in self.data... skipping!' % resource)
            return

        data = list()
        for obj_uid, obj in self.data[resource].objects.items():
            data += obj.get_zabbix_discovery_data()

        if data:
            metric = obj.get_discovery_for_zabbix(data)
            self.logger.debug('send_zabbix_discovery: resource "%s": %s' % (resource, metric))
            self.send_discovery_to_zabbix(resource, metric=metric)
        else:
            self.logger.warning('send_zabbix_discovery: resource "%s" has no discovery data' % resource)

        self.discovery_sent[resource] = datetime.now()
        if resource == 'pods' and self.config.container_crawling == 'container':
            self.discovery_sent['containers'] = datetime.now()

    def send_object(self, resource: str, resourced_obj: K8sObject,
                    event_type: str, send_zabbix_data: bool = False,
                    send_web: bool = False) -> None:
        # send single object for updates
        if send_zabbix_data:
            if resourced_obj.last_sent_zabbix < datetime.now() - timedelta(seconds=self.rate_limit_seconds):
                self.send_data_to_zabbix(resource, obj=resourced_obj)
                resourced_obj.last_sent_zabbix = datetime.now()
                resourced_obj.is_dirty_zabbix = False
            else:
                self.logger.debug(
                    "obj >>>type: %s, name: %s/%s<<< not sending to zabbix! rate limited (%is)"
                    % (resource, resourced_obj.name_space, resourced_obj.name, self.rate_limit_seconds)
                )
                resourced_obj.is_dirty_zabbix = True

        if send_web:
            if resourced_obj.last_sent_web < datetime.now() - timedelta(seconds=self.rate_limit_seconds):
                self.send_to_web_api(resource, resourced_obj, event_type)
                resourced_obj.last_sent_web = datetime.now()
                if resourced_obj.is_dirty_web is True and not send_zabbix_data:
                    # only set dirty False if send_to_web_api worked
                    resourced_obj.is_dirty_web = False
            else:
                self.logger.debug(
                    "obj >>>type: %s, name: %s/%s<<< not sending to web! rate limited (%is)"
                    % (resource, resourced_obj.name_space, resourced_obj.name, self.rate_limit_seconds)
                )
                resourced_obj.is_dirty_web = True

    def send_heartbeat_info(self, resource: str) -> None:
        result = self.send_to_zabbix([
            ZabbixMetric(self.zabbix_host, 'check_kubernetesd[discover,api]', str(int(time.time())))
        ])
        if result.failed > 0:
            self.logger.error(f"{resource} failed to send heartbeat to zabbix")
        else:
            self.logger.debug(f"{resource} successfully sent heartbeat to zabbix ")

    def send_to_zabbix(self, metrics: list[ZabbixMetric]) -> ZabbixResponse | DryResult:
        if self.zabbix_dry_run:
            result = DryResult()
        else:
            try:
                result = self.zabbix_sender.send(metrics)
            except Exception as e:
                self.logger.error(e)
                result = DryResult()
                result.failed = 1
                result.processed = 0

        if self.zabbix_debug:
            if len(metrics) > 1:
                self.logger.info('===> Sending to zabbix: >>>\n%s\n<<<' % pformat(metrics, indent=2))
            else:
                self.logger.info('===> Sending to zabbix: >>>%s<<<' % metrics)
        return result

    def send_discovery_to_zabbix(self, resource: str, metric: ZabbixMetric | list = None,
                                 obj: K8sObject | None = None) -> None:
        if resource not in self.zabbix_resources:
            self.logger.warning(
                f'resource {resource} ist not activated, active resources are : {",".join(self.zabbix_resources)}')
            return

        if obj:
            discovery_data = obj.get_discovery_for_zabbix(metric)
            if not discovery_data:
                self.logger.warning('No discovery_data for obj %s, not sending to zabbix!' % obj.uid)
                return

            discovery_key = 'check_kubernetesd[discover,' + resource + ']'
            result = self.send_to_zabbix([ZabbixMetric(host=self.zabbix_host, key=discovery_key, value=discovery_data)])
            if result.failed > 0:
                self.logger.error("failed to send zabbix discovery: %s : >>>%s<<<" % (discovery_key, discovery_data))
            elif self.zabbix_debug:
                self.logger.info("successfully sent zabbix discovery: %s  >>>>%s<<<" % (discovery_key, discovery_data))
        elif metric:
            if isinstance(metric, list):
                result = self.send_to_zabbix(metric)
            else:
                result = self.send_to_zabbix([metric])
            if result.failed > 0:
                self.logger.error("failed to send mass zabbix discovery: >>>%s<<<" % metric)
            elif self.zabbix_debug:
                self.logger.info("successfully sent mass zabbix discovery: >>>%s<<<" % metric)
        else:
            self.logger.warning("No obj or metrics found for send_discovery_to_zabbix [%s]" % resource)

    def send_data_to_zabbix(self, resource: str, obj: K8sObject | None = None,
                            metrics: list[ZabbixMetric] | None = None) -> None:

        if resource not in self.discovery_sent:
            self.logger.info('skipping send_data_to_zabbix for %s, discovery not send yet!' % resource)
            return
        elif obj and obj.added > self.discovery_sent[resource]:
            self.logger.info(
                f'skipping send of {obj}, resource {resource} discovery_sent "{self.discovery_sent[resource]}" '
                f'is older than obj: {obj.added.isoformat()}')
            return
        else:
            self.logger.info(f'sending data for "{resource}" to zabbix')

        if metrics is None:
            metrics = list()
        if resource not in self.zabbix_resources:
            return

        if obj and len(metrics) == 0:
            metrics = obj.get_zabbix_metrics()

        if len(metrics) == 0 and obj:
            self.logger.debug("No zabbix metrics to send for %s: %s" % (obj.uid, metrics))
            return
        elif len(metrics) == 0:
            self.logger.debug("No zabbix metrics or no obj found for [%s]" % resource)
            return

        if self.zabbix_single_debug:
            for metric in metrics:
                result = self.send_to_zabbix([metric])
                self.logger.debug("Failed metrics: %s" % (result))
                if result.failed > 0:
                    self.logger.error("failed to send zabbix items: %s", metric)
                else:
                    self.logger.info("successfully sent zabbix items: %s", metric)
        else:
            result = self.send_to_zabbix(metrics)
            if result.failed > 0:
                self.logger.error(
                    "failed to send %s zabbix items, processed %s items [%s: %s]"
                    % (result.failed, result.processed, resource, obj.name if obj else "metrics")
                )
                self.logger.debug("Result: %s" % (result))
            else:
                self.logger.debug(
                    "successfully sent %s zabbix items [%s: %s]"
                    % (len(metrics), resource, obj.name if obj else "metrics")
                )

    def send_to_web_api(self, resource: str, obj: K8sObject, action: str) -> None:
        if resource not in self.web_api_resources:
            return

        if self.web_api_enable:
            api = self.get_web_api()
            data_to_send = obj.resource_data
            data_to_send["cluster"] = self.web_api_cluster

            api.send_data(resource, data_to_send, action)
        else:
            self.logger.debug("suppressing submission of %s %s/%s" % (resource, obj.name_space, obj.name))
