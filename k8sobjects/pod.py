import json
import sys
import logging
import re
from pprint import pformat

from pyzabbix import ZabbixMetric

from k8sobjects import K8sObject, transform_value

logger = logging.getLogger(__file__)


class Pod(K8sObject):
    object_type = 'pod'
    kind = None

    @property
    def name(self) -> str:
        if 'metadata' not in self.data and 'name' in self.data['metadata']:
            raise Exception(f'Could not find name in metadata for resource {self.resource}')

        if "owner_references" in self.data['metadata']:
            try:
                self.kind = self.data['metadata']['owner_references'][0]['kind']
            except:
                self.kind = None
        generate_name = self.data['metadata']['name']
        if "generate_name" in self.data['metadata'] and self.data['metadata']['generate_name']:
            generate_name = self.data['metadata']['generate_name']
        match self.kind:
            case "Job":
                name = re.sub(r'-\d+-$', '', generate_name)
            case "ReplicaSet":
                name = re.sub(r'-[a-f0-9]{4,}-$', '', generate_name)
            case _:
                try:
                    name = re.sub(r'-$', '', generate_name)
                except Exception as e:
                    sys.stderr.write("STATUS_NAME kind:%s\ngenerate_name:%s\ndata:%s\n" % (self.kind, generate_name, pformat(self.data, indent=2)))
        self.base_name = name
        return name


    def get_zabbix_discovery_data(self) -> list[dict[str, str]]:
        # Main Methode
        data = super().get_zabbix_discovery_data()
        data[0]['{#KIND}'] = self.kind
        for container in self.containers:
            data += [ 
                { 
                    "{#NAMESPACE}": self.name_space,
                    "{#NAME}": self.base_name,
                    "{#CONTAINER}": container,
                }
            ]
        return data


    def get_zabbix_metrics(self):
        data = self.resource_data
        data_to_send = list()

        sys.stderr.write("STATUS_METRICS data: %s\n" % (data))

        for status_type in self.data["status"]:
            if status_type in self.resource_data: 
                data_to_send.append(ZabbixMetric(
                    self.zabbix_host,
                    'check_kubernetesd[get,pods,%s,%s,%s]' % (self.name_space, self.name, status_type),
                    transform_value(self.resource_data[status_type]))
                )
        if "available_status" in self.resource_data:
            data_to_send.append(ZabbixMetric(
                self.zabbix_host,
                'check_kubernetesd[get,pods,%s,%s,available_status]' % (self.name_space, self.name),
                self.resource_data['available_status']))

        return data_to_send


    @property
    def resource_data(self):


        data = super().resource_data
        data["containers"] = json.dumps(self.containers)
        container_status = dict()
        data["ready"] = True
        pod_data = {
            "restart_count": 0,
            "ready": 0,
            "not_ready": 0,
            "status": "OK",
        }
        self.phase = self.data["status"]["phase"]

        if "container_statuses" in self.data["status"] and self.data["status"]["container_statuses"]:
            for container in self.data["status"]["container_statuses"]:
                status_values = []
                container_name = container["name"]

                # this pod data
                if container_name not in container_status:
                    container_status[container_name] = {
                        "restart_count": 0,
                        "ready": 0,
                        "not_ready": 0,
                        "status": "OK",
                    }
                container_status[container_name]["restart_count"] += container["restart_count"]
                pod_data["restart_count"] += container["restart_count"]

                if container["ready"] is True:
                    container_status[container_name]["ready"] += 1
                    pod_data["ready"] += 1
                # There are 5 possible Pod phases: Pending, Running, Succeeded, Failed, Unknown
                # Only Failed and Unknown should throw an Error
                elif self.phase not in ["Succeeded", "Running", "Pending"]:
                    container_status[container_name]["not_ready"] += 1
                    pod_data["not_ready"] += 1

                if container["state"] and len(container["state"]) > 0:
                    for status, container_data in container["state"].items():
                        terminated_state = ""
                        try:
                            terminated_state = container["state"]["terminated"]["reason"]
                        except:
                            pass
                        # There are three possible container states: Waiting, Running, and Terminated.
                        # not status in ["waiting", "running"]
                        if container_data and status == "terminated" and terminated_state != "Completed":
                            status_values.append(status)

                if len(status_values) > 0:
                    logger.debug("Pod STATUS_ERR: %s\n%s\n" % (status_values, container))
                    container_status[container_name]["status"] = "ERROR: " + (",".join(status_values))
                    pod_data["status"] = container_status[container_name]["status"]
                    data["ready"] = False

        data["container_status"] = json.dumps(container_status)
        data["pod_data"] = json.dumps(pod_data)
        logger.debug("Pod STATUS: data:\n%s\n" % (data))
        return data




    @property
    def containers(self):
        containers = {}
        for container in self.data["spec"]["containers"]:
            containers.setdefault(container["name"], 0)
            containers[container["name"]] += 1
        return containers

    def get_discovery_for_zabbix(self, discovery_data=None):
        # Alte Methode
        if discovery_data is None:
            discovery_data = self.get_zabbix_discovery_data()

        return ZabbixMetric(
            self.zabbix_host,
            "check_kubernetesd[discover,containers]",
            json.dumps(
                {
                    "data": discovery_data,
                }
            ),
        )

