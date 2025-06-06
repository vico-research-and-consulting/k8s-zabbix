import datetime
import hashlib
import importlib
import json
import logging
import re
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from k8sobjects.k8sresourcemanager import K8sResourceManager

from pyzabbix import ZabbixMetric

logger = logging.getLogger("k8s-zabbix")

K8S_RESOURCES = dict(
    nodes="node",
    components="component",
    services="service",
    deployments="deployment",
    statefulsets="statefulset",
    daemonsets="daemonset",
    pods="pod",
    containers="container",
    secrets="secret",
    ingresses="ingress",
    pvcs="pvc",
)

INITIAL_DATE = datetime.datetime(2000, 1, 1, 0, 0)


def json_encoder(obj: object) -> str:
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    raise TypeError(f"custom json_encoder: unable to encode {type(obj)}")


def transform_value(value: str) -> str:
    if value is None:
        return "0"
    m = re.match(r'^(\d+)(Ki)$', str(value))
    if m:
        if m.group(2) == "Ki":
            return str(int(float(m.group(1)) * 1024))

    m = re.match(r'^(\d+)(m)$', str(value))
    if m:
        if m.group(2) == "m":
            return str(float(m.group(1)) / 1000)

    if value is None:
        return "0"

    return value


def slugit(name_space: str, name: str, maxlen: int) -> str:
    if name_space:
        slug = name_space + "/" + name
    else:
        slug = name

    if len(slug) <= maxlen:
        return slug

    prefix_pos = int((maxlen / 2) - 1)
    suffix_pos = len(slug) - int(maxlen / 2) - 2
    return slug[:prefix_pos] + "~" + slug[suffix_pos:]


class MetadataObjectType(TypedDict):
    name: str
    namespace: str
    generate_name: str | None
    owner_references: list[dict[str, str]]


class ObjectDataType(TypedDict):
    metadata: MetadataObjectType
    item: dict[str, dict]


def calculate_checksum_for_dict(data: ObjectDataType) -> str:
    json_str = json.dumps(
        data,
        sort_keys=True,
        default=json_encoder,
        indent=2
    )
    checksum = hashlib.md5(json_str.encode('utf-8')).hexdigest()
    return checksum


class K8sObject:
    """Holds the resource data"""
    object_type: str = "UNDEFINED"

    def __init__(self, obj_data: ObjectDataType, resource: str, manager: 'K8sResourceManager'):
        """Get the resource data from the k8s api"""
        self.is_dirty_zabbix = True
        self.is_dirty_web = True
        self.added = INITIAL_DATE
        self.last_sent_zabbix_discovery = INITIAL_DATE
        self.last_sent_zabbix = INITIAL_DATE
        self.last_sent_web = INITIAL_DATE
        self.resource = resource
        self.data = obj_data
        self.data_checksum = calculate_checksum_for_dict(obj_data)
        self.manager = manager
        self.zabbix_host = self.manager.zabbix_host

    def __str__(self) -> str:
        return self.uid

    @property
    def resource_data(self) -> dict[str, str]:
        """ customized values for k8s objects """
        if self.name_space is None:
            if self.resource.lower() not in ["nodes", "components"]:
                raise RuntimeError(f"name_space is None for [{self.resource}] {self.name}")
        return dict(
            name=self.name,
            name_space=self.name_space
        )

    @property
    def uid(self) -> str:
        if not hasattr(self, 'object_type'):
            raise AttributeError('No object_type set! Dont use K8sObject itself!')
        elif not self.name:
            raise AttributeError(
                "No name set for K8sObject.uid! [%s] name_space: %s, name: %s"
                % (self.object_type, self.name_space, self.name)
            )

        if self.name_space:
            return self.object_type + "_" + self.name_space + "_" + self.name
        return self.object_type + "_" + self.name

    @property
    def name(self) -> str:
        """The name of the object"""
        if 'metadata' in self.data and 'name' in self.data['metadata']:
            return self.data['metadata']['name']
        else:
            raise Exception(f'Could not find name in metadata for resource {self.resource}')

    @property
    def name_space(self) -> str | None:
        from .component import Component
        from .node import Node
        if isinstance(self, Node) or isinstance(self, Component):
            return None

        name_space = self.data.get("metadata", {}).get("namespace")
        if not name_space:
            raise Exception("Could not find name_space for obj [%s] %s" % (self.resource, self.name))
        return name_space

    def slug(self, name):
        return slugit(self.name_space or "None", name, 40)

    def get_uid_list_and_data(self, data=None):
        uid_ret = []
        data_ret = {}

        # if no data was fetched and passed before: fetch it
        if self.resource == 'pvcs':
            obj_list = self.get_list()
        else:
            obj_list = self.get_list().items

        for obj in obj_list:
            if self.resource == 'pvcs':
                d = obj
                uid_ret.append(d.uid)
                data_ret[d.uid] = d
            else:
                d = obj.to_dict()
                n = self.manager.resource_class(d, self.resource, manager=self.manager)
                uid_ret.append(n.uid)
                data_ret[n.uid] = n
        return uid_ret, data_ret

    def is_unsubmitted_web(self) -> bool:
        return self.last_sent_web == INITIAL_DATE

    def is_unsubmitted_zabbix(self) -> bool:
        return self.last_sent_zabbix == INITIAL_DATE

    def is_unsubmitted_zabbix_discovery(self) -> bool:
        return self.last_sent_zabbix_discovery == datetime.datetime(2000, 1, 1, 0, 0)

    def get_zabbix_discovery_data(self) -> list[dict[str, str]]:
        return [{
            "{#NAME}": self.name,
            "{#NAMESPACE}": self.name_space or "None",
            "{#SLUG}": self.slug(self.name),
        }]

    def get_discovery_for_zabbix(self, discovery_data: list[dict[str, str]] | None) -> ZabbixMetric:
        if discovery_data is None:
            discovery_data = self.get_zabbix_discovery_data()

        return ZabbixMetric(
            self.zabbix_host,
            "check_kubernetesd[discover,%s]" % self.resource,
            json.dumps(
                {
                    "data": discovery_data,
                }
            ),
        )

    def get_zabbix_metrics(self) -> list[ZabbixMetric]:
        logger.fatal(f"get_zabbix_metrics: not implemented for {self.object_type}")
        return []
