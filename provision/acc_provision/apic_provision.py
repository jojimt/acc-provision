from __future__ import print_function, unicode_literals

import collections
import json
import sys
import re
import requests
import urllib3
import ipaddress

debug_http = False
if debug_http:
    import logging
    import http.client as http_client
    http_client.HTTPConnection.debuglevel = 1
    # You must initialize logging, otherwise you'll not see debug output.
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    from requests.packages.urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
except Exception:
    pass
apic_debug = False
apic_cookies = {}
apic_default_timeout = (15, 90)
aciContainersOwnerAnnotation = "orchestrator:aci-containers-controller"
aci_prefix = "aci-containers-"


def err(msg):
    print("ERR:  " + msg, file=sys.stderr)


def warn(msg):
    print("WARN: " + msg, file=sys.stderr)


def dbg(msg):
    if apic_debug:
        print("DBG:  " + msg, file=sys.stderr)


def yesno(flag):
    if flag:
        return "yes"
    return "no"


def aci_obj(klass, pair_list):
    kwargs = collections.OrderedDict(pair_list)
    children = kwargs.pop("_children", None)
    data = collections.OrderedDict(
        [(klass, collections.OrderedDict([("attributes", kwargs)]))]
    )
    if children:
        data[klass]["children"] = children
    return data


class Apic(object):

    TENANT_OBJECTS = ["ap-kubernetes", "BD-kube-node-bd", "BD-kube-pod-bd", "brc-kube-api", "brc-health-check", "brc-dns", "brc-icmp", "flt-kube-api-filter", "flt-dns-filter", "flt-health-check-filter-out", "flt-icmp-filter", "flt-health-check-filter-in"]
    ACI_PREFIX = aci_prefix

    def __init__(
        self,
        addr,
        username,
        password,
        ssl=True,
        verify=False,
        timeout=None,
        debug=False,
        capic=False
    ):
        global apic_debug
        apic_debug = debug
        self.addr = addr
        self.ssl = ssl
        self.username = username
        self.password = password
        self.cookies = apic_cookies.get((addr, username, ssl))
        self.errors = 0
        self.verify = verify
        self.timeout = timeout if timeout else apic_default_timeout
        self.debug = debug
        self.capic = capic

        if self.cookies is None:
            self.login()
            if self.cookies is not None:
                apic_cookies[(addr, username, ssl)] = self.cookies

    def url(self, path):
        if self.ssl:
            return "https://%s%s" % (self.addr, path)
        return "http://%s%s" % (self.addr, path)

    def get(self, path, data=None, params=None):
        args = dict(data=data, cookies=self.cookies, verify=self.verify, params=params)
        args.update(timeout=self.timeout)
        dbg("getting path: {} {}".format(path, json.dumps(args)))
        return requests.get(self.url(path), **args)

    def post(self, path, data):
        if self.capic:
            args = dict(json=data, cookies=self.cookies, verify=self.verify)
        else:
            # APIC seems to accept request body as form-encoded
            args = dict(data=data, cookies=self.cookies, verify=self.verify)
        args.update(timeout=self.timeout)
        dbg("posting {}".format(json.dumps(args)))
        return requests.post(self.url(path), **args)

    def delete(self, path, data=None):
        args = dict(data=data, cookies=self.cookies, verify=self.verify)
        args.update(timeout=self.timeout)
        return requests.delete(self.url(path), **args)

    def login(self):
        data = '{"aaaUser":{"attributes":{"name": "%s", "pwd": "%s"}}}' % (
            self.username,
            self.password,
        )
        path = "/api/aaaLogin.json"
        req = requests.post(self.url(path), data=data, verify=False)
        if req.status_code == 200:
            resp = json.loads(req.text)
            dbg("Login resp: {}".format(req.text))
            token = resp["imdata"][0]["aaaLogin"]["attributes"]["token"]
            self.cookies = collections.OrderedDict([("APIC-Cookie", token)])
        else:
            print("Login failed - {}".format(req.text))
            print("Addr: {} u: {} p: {}".format(self.addr, self.username, self.password))
        return req

    def check_resp(self, resp):
        respj = json.loads(resp.text)
        if len(respj["imdata"]) > 0:
            ret = respj["imdata"][0]
            if "error" in ret:
                raise Exception("APIC REST Error: %s" % ret["error"])
        return resp

    def get_path(self, path, multi=False):
        ret = None
        try:
            resp = self.get(path)
            self.check_resp(resp)
            respj = json.loads(resp.text)
            if len(respj["imdata"]) > 0:
                if multi:
                    ret = respj["imdata"]
                else:
                    ret = respj["imdata"][0]
        except Exception as e:
            self.errors += 1
            err("Error in getting %s: %s: " % (path, str(e)))
        return ret

    def get_infravlan(self):
        infra_vlan = None
        path = (
            "/api/node/mo/uni/infra/attentp-default/provacc" +
            "/rsfuncToEpg-[uni/tn-infra/ap-access/epg-default].json"
        )
        data = self.get_path(path)
        if data:
            encap = data["infraRsFuncToEpg"]["attributes"]["encap"]
            infra_vlan = int(encap.split("-")[1])
        return infra_vlan

    def get_aep(self, aep_name):
        path = "/api/mo/uni/infra/attentp-%s.json" % aep_name
        return self.get_path(path)

    def get_vrf(self, tenant, name):
        path = "/api/mo/uni/tn-%s/ctx-%s.json" % (tenant, name)
        return self.get_path(path)

    def get_l3out(self, tenant, name):
        path = "/api/mo/uni/tn-%s/out-%s.json" % (tenant, name)
        return self.get_path(path)

    def check_l3out_vrf(self, tenant, name, vrf_name):
        path = "/api/mo/uni/tn-%s/out-%s/rsectx.json?query-target=self" % (tenant, name)
        res = False
        try:
            tDn = self.get_path(path)["l3extRsEctx"]["attributes"]["tDn"]
            vrf_dn = "uni/tn-%s/ctx-%s" % (tenant, vrf_name)
            res = (tDn == vrf_dn)
        except Exception as e:
            err("Error in getting configured vrf for %s/%s: %s" % (tenant, name, str(e)))
        return res

    def get_user(self, name):
        path = "/api/node/mo/uni/userext/user-%s.json" % name
        return self.get_path(path)

    def get_ap(self, tenant):
        path = "/api/mo/uni/tn-%s/ap-kubernetes.json" % tenant
        return self.get_path(path)

    def provision(self, data, sync_login):
        ignore_list = []
        if self.get_user(sync_login):
            warn("User already exists (%s), recreating user" % sync_login)
            user_path = "/api/node/mo/uni/userext/user-%s.json" % sync_login
            resp = self.delete(user_path)
            dbg("%s: %s" % (user_path, resp.text))

        for path, config in data:
            try:
                if path in ignore_list:
                    continue
                if config is not None:
                    resp = self.post(path, config)
                    self.check_resp(resp)
                    dbg("%s: %s" % (path, resp.text))
            except Exception as e:
                # log it, otherwise ignore it
                self.errors += 1
                err("Error in provisioning %s: %s" % (path, str(e)))

    def unprovision(self, data, system_id, tenant, vrf_tenant, cluster_tenant, old_naming):
        cluster_tenant_path = "/api/mo/uni/tn-%s.json" % cluster_tenant
        shared_resources = ["/api/mo/uni/infra.json", "/api/mo/uni/tn-common.json", cluster_tenant_path]

        if vrf_tenant not in ["common", system_id]:
            shared_resources.append("/api/mo/uni/tn-%s.json" % vrf_tenant)

        try:
            for path, config in data:
                if path.split("/")[-1].startswith("instP-"):
                    continue
                if path not in shared_resources:
                    resp = self.delete(path)
                    self.check_resp(resp)
                    dbg("%s: %s" % (path, resp.text))
                else:
                    if path == cluster_tenant_path:
                        path += "?query-target=children"
                        resp = self.get(path)
                        self.check_resp(resp)
                        respj = json.loads(resp.text)
                        respj = respj["imdata"]
                        for resp in respj:
                            for val in resp.values():
                                if 'rsTenantMonPol' not in val['attributes']['dn'] and 'svcCont' not in val['attributes']['dn']:
                                    del_path = "/api/node/mo/" + val['attributes']['dn'] + ".json"
                                    if 'name' in val['attributes']:
                                        name = val['attributes']['name']
                                        if (not old_naming) and (system_id in name):
                                            resp = self.delete(del_path)
                                            self.check_resp(resp)
                                            dbg("%s: %s" % (del_path, resp.text))
            if old_naming:
                for object in self.TENANT_OBJECTS:
                    del_path = "/api/node/mo/uni/tn-%s/%s.json" % (cluster_tenant, object)
                    resp = self.delete(del_path)
                    self.check_resp(resp)
                    dbg("%s: %s" % (del_path, resp.text))

        except Exception as e:
            # log it, otherwise ignore it
            self.errors += 1
            err("Error in un-provisioning %s: %s" % (path, str(e)))

        # Clean the cluster tenant iff it has our annotation and does
        # not have any application profiles
        if self.check_valid_annotation(cluster_tenant_path) and self.check_no_ap(cluster_tenant_path):
            self.delete(cluster_tenant_path)

        # Finally clean any stray resources in common
        self.clean_tagged_resources(system_id, tenant)

    def check_valid_annotation(self, path):
        data = self.get_path(path)
        if data['fvTenant']['attributes']['annotation'] == aciContainersOwnerAnnotation:
            return True
        return False

    def check_no_ap(self, path):
        path += "?query-target=children"
        if 'fvAp' in self.get_path(path):
            return False
        return True

    def valid_tagged_resource(self, tag, system_id, tenant):
        ret = False
        prefix = "%s-" % system_id
        if tag.startswith(prefix):
            tagid = tag[len(prefix):]
            if len(tagid) == 32:
                try:
                    int(tagid, base=16)
                    ret = True
                except ValueError:
                    ret = False
        return ret

    def clean_tagged_resources(self, system_id, tenant):

        try:
            mos = collections.OrderedDict([])
            # collect tagged resources
            tags = collections.OrderedDict([])
            tags_path = "/api/node/mo/uni/tn-%s.json" % (tenant,)
            tags_path += "?query-target=subtree&target-subtree-class=tagInst"
            tags_list = self.get_path(tags_path, multi=True)
            if tags_list is not None:
                for tag_mo in tags_list:
                    tag_name = tag_mo["tagInst"]["attributes"]["name"]
                    if self.valid_tagged_resource(tag_name, system_id, tenant):
                        tags[tag_name] = True
                        dbg("Deleting tag: %s" % tag_name)
                    else:
                        dbg("Ignoring tag: %s" % tag_name)

            for tag in tags.keys():
                dbg("Objcts selected for tag: %s" % tag)
                mo_path = "/api/tag/%s.json" % tag
                mo_list = self.get_path(mo_path, multi=True)
                for mo_dict in mo_list:
                    for mo_key in mo_dict.keys():
                        mo = mo_dict[mo_key]
                        mo_dn = mo["attributes"]["dn"]
                        mos[mo_dn] = True
                        dbg("    - %s" % mo_dn)

            # collect resources with annotation
            annot_path = "/api/node/mo/uni/tn-%s.json" % (tenant,)
            annot_path += "?query-target=subtree&target-subtree-class=tagAnnotation"
            annot_list = self.get_path(annot_path, multi=True)
            if annot_list is not None:
                for tag_mo in annot_list:
                    tag_name = tag_mo["tagAnnotation"]["attributes"]["value"]
                    if self.valid_tagged_resource(tag_name, system_id, tenant):
                        dbg("Deleting tag: %s" % tag_name)
                        parent_dn = tag_mo["tagAnnotation"]["attributes"]["dn"]
                        reg = re.search('(.*)(/annotationKey.*)', parent_dn)
                        dn_name = reg.group(1)
                        dn_path = "/api/node/mo/" + dn_name + ".json"
                        resp = self.get(dn_path)
                        self.check_resp(resp)
                        respj = json.loads(resp.text)
                        ret = respj["imdata"][0]
                        for obj, att in ret.items():
                            if att["attributes"]["annotation"] == "orchestrator:aci-containers-controller":
                                mos[dn_name] = True
                            else:
                                dbg("Ignoring tag: %s" % tag_name)

            for mo_dn in sorted(mos.keys(), reverse=True):
                mo_path = "/api/node/mo/%s.json" % mo_dn
                dbg("Deleting object: %s" % mo_dn)
                self.delete(mo_path)

        except Exception as e:
            self.errors += 1
            err("Error in deleting tags: %s" % str(e))


class ApicKubeConfig(object):

    ACI_PREFIX = aci_prefix

    def __init__(self, config):
        self.config = config
        self.use_kubeapi_vlan = True
        if self.config["aci_config"]["use_legacy_kube_naming_convention"]:
            self.tenant_generator = "kube_tn"  # use the older kube naming convention
        else:
            self.tenant_generator = "cluster_tn"
        self.associate_aep_to_nested_inside_domain = False

    def get_nested_domain_type(self):
        inside = self.config["aci_config"]["vmm_domain"].get("nested_inside")
        if not inside:
            return None
        t = inside.get("type")
        if t and t.lower() == "vmware":
            return "VMware"
        return t

    @staticmethod
    def save_config(config, outfilep):
        for path, data in config:
            print(path, file=outfilep)
            print(data, file=outfilep)

    def get_config(self):
        def assert_attributes_is_first_key(data):
            """Check that attributes is the first key in the JSON."""
            if isinstance(data, collections.Mapping) and "attributes" in data:
                assert next(iter(data.keys())) == "attributes"
                for item in data.items():
                    assert_attributes_is_first_key(item)
            elif isinstance(data, (list, tuple)):
                for item in data:
                    assert_attributes_is_first_key(item)

        def update(data, x):
            if x:
                assert_attributes_is_first_key(x)
                data.append((x[0], json.dumps(
                    x[1],
                    indent=4,
                    separators=(",", ": "))))
                for path in x[2:]:
                    data.append((path, None))

        data = []
        update(data, self.pdom_pool())
        update(data, self.vdom_pool())
        update(data, self.mcast_pool())
        update(data, self.phys_dom())
        update(data, self.kube_dom())
        update(data, self.nested_dom())
        update(data, self.associate_aep())
        update(data, self.opflex_cert())

        update(data, self.l3out_tn())
        update(data, getattr(self, self.tenant_generator)(self.config['flavor']))
        for l3out_instp in self.config["aci_config"]["l3out"]["external_networks"]:
            update(data, self.l3out_contract(l3out_instp))

        update(data, self.kube_user())
        update(data, self.kube_cert())
        if self.config["aci_config"]["netflow_exporter"]["enable"]:
            update(data, self.netflow_exporter())
            update(data, self.netflow_cont())
        return data

    def annotateApicObjects(self, data, pre_existing_tenant=False):
        for key, value in data.items():
            if "children" in value.keys():
                children = value["children"]
                for i in range(len(children)):
                    self.annotateApicObjects(children[i])
            break
        if not key == "fvTenant":
            data[key]["attributes"]["annotation"] = aciContainersOwnerAnnotation
        elif not (data[key]["attributes"]["name"] == "common") and not (pre_existing_tenant):
            data[key]["attributes"]["annotation"] = aciContainersOwnerAnnotation

    def pdom_pool(self):
        pool_name = self.config["aci_config"]["physical_domain"]["vlan_pool"]
        service_vlan = self.config["net_config"]["service_vlan"]

        path = "/api/mo/uni/infra/vlanns-[%s]-static.json" % pool_name
        data = collections.OrderedDict(
            [
                (
                    "fvnsVlanInstP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", pool_name), ("allocMode", "static")]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvnsEncapBlk",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "allocMode",
                                                                        "static",
                                                                    ),
                                                                    (
                                                                        "from",
                                                                        "vlan-%s"
                                                                        % service_vlan,
                                                                    ),
                                                                    (
                                                                        "to",
                                                                        "vlan-%s"
                                                                        % service_vlan,
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        if self.use_kubeapi_vlan:
            kubeapi_vlan = self.config["net_config"]["kubeapi_vlan"]
            data["fvnsVlanInstP"]["children"].insert(
                0,
                collections.OrderedDict(
                    [
                        (
                            "fvnsEncapBlk",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                ("allocMode", "static"),
                                                ("from", "vlan-%s" % kubeapi_vlan),
                                                ("to", "vlan-%s" % kubeapi_vlan),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                ),
            )
        self.annotateApicObjects(data)
        return path, data

    def vdom_pool(self):
        encap_type = self.config["aci_config"]["vmm_domain"]["encap_type"]
        vpool_name = self.config["aci_config"]["vmm_domain"]["vlan_pool"]
        vlan_range = self.config["aci_config"]["vmm_domain"]["vlan_range"]

        if encap_type != "vlan":
            return None

        path = "/api/mo/uni/infra/vlanns-[%s]-dynamic.json" % vpool_name
        data = collections.OrderedDict(
            [
                (
                    "fvnsVlanInstP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", vpool_name), ("allocMode", "dynamic")]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvnsEncapBlk",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "allocMode",
                                                                        "dynamic",
                                                                    ),
                                                                    (
                                                                        "from",
                                                                        "vlan-%s"
                                                                        % vlan_range[
                                                                            "start"
                                                                        ],
                                                                    ),
                                                                    (
                                                                        "to",
                                                                        "vlan-%s"
                                                                        % vlan_range[
                                                                            "end"
                                                                        ],
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def mcast_pool(self):
        mpool_name = self.config["aci_config"]["vmm_domain"]["mcast_pool"]
        mcast_start = self.config["aci_config"]["vmm_domain"]["mcast_range"]["start"]
        mcast_end = self.config["aci_config"]["vmm_domain"]["mcast_range"]["end"]

        path = "/api/mo/uni/infra/maddrns-%s.json" % mpool_name
        data = collections.OrderedDict(
            [
                (
                    "fvnsMcastAddrInstP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", mpool_name),
                                        ("dn", "uni/infra/maddrns-%s" % mpool_name),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvnsMcastAddrBlk",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "from",
                                                                        mcast_start,
                                                                    ),
                                                                    ("to", mcast_end),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def netflow_exporter(self):
        exp_name = self.config["aci_config"]["netflow_exporter"]["name"]
        version = self.config["aci_config"]["netflow_exporter"]["ver"]
        dstAddr = self.config["aci_config"]["netflow_exporter"]["dstAddr"]
        dstPort = self.config["aci_config"]["netflow_exporter"]["dstPort"]
        srcAddr = self.config["aci_config"]["netflow_exporter"]["srcAddr"]

        path = "api/mo/uni/infra/vmmexporterpol-%s.json" % exp_name
        data = collections.OrderedDict(
            [
                (
                    "netflowVmmExporterPol",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", exp_name),
                                        ("ver", version),
                                        ("dstAddr", dstAddr),
                                        ("dstPort", dstPort),
                                        ("srcAddr", srcAddr),
                                    ]
                                ),
                            ),
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def netflow_cont(self):
        exp_name = self.config["aci_config"]["netflow_exporter"]["name"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        activeFlowTimeOut = self.config["aci_config"]["netflow_exporter"]["activeFlowTimeOut"]

        path = "api/node/mo/uni/vmmp-Kubernetes/dom-%s/vswitchpolcont.json" % vmm_name
        data = collections.OrderedDict(
            [
                (
                    "vmmVSwitchPolicyCont",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vmmRsVswitchExporterPol",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    ("activeFlowTimeOut", activeFlowTimeOut),
                                                                    ("tDn", "/uni/infra/vmmexporterpol-%s" % exp_name),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ]
                            ),
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def phys_dom(self):
        phys_name = self.config["aci_config"]["physical_domain"]["domain"]
        pool_name = self.config["aci_config"]["physical_domain"]["vlan_pool"]

        path = "/api/mo/uni/phys-%s.json" % phys_name
        data = collections.OrderedDict(
            [
                (
                    "physDomP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("dn", "uni/phys-%s" % phys_name),
                                        ("name", phys_name),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "infraRsVlanNs",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "tDn",
                                                                        "uni/infra/vlanns-[%s]-static"
                                                                        % pool_name,
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def kube_dom(self):
        vmm_type = self.config["aci_config"]["vmm_domain"]["type"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        encap_type = self.config["aci_config"]["vmm_domain"]["encap_type"]
        mcast_fabric = self.config["aci_config"]["vmm_domain"]["mcast_fabric"]
        mpool_name = self.config["aci_config"]["vmm_domain"]["mcast_pool"]
        vpool_name = self.config["aci_config"]["vmm_domain"]["vlan_pool"]
        kube_controller = self.config["kube_config"]["controller"]

        mode = "k8s"
        scope = "kubernetes"
        if vmm_type == "OpenShift":
            mode = "openshift"
            scope = "openshift"
        elif vmm_type == "CloudFoundry":
            mode = "cf"
            scope = "cloudfoundry"

        path = "/api/mo/uni/vmmp-%s/dom-%s.json" % (vmm_type, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "vmmDomP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", vmm_name),
                                        ("mode", mode),
                                        ("enfPref", "sw"),
                                        ("encapMode", encap_type),
                                        ("prefEncapMode", encap_type),
                                        ("mcastAddr", mcast_fabric),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vmmCtrlrP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    ("name", vmm_name),
                                                                    ("mode", mode),
                                                                    ("scope", scope),
                                                                    (
                                                                        "hostOrIp",
                                                                        kube_controller,
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vmmRsDomMcastAddrNs",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "tDn",
                                                                        "uni/infra/maddrns-%s"
                                                                        % mpool_name,
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        if encap_type == "vlan":
            vlan_pool_data = collections.OrderedDict(
                [
                    (
                        "infraRsVlanNs",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [
                                            (
                                                "tDn",
                                                "uni/infra/vlanns-[%s]-dynamic"
                                                % vpool_name,
                                            )
                                        ]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            )
            data["vmmDomP"]["children"].append(vlan_pool_data)
        self.annotateApicObjects(data)
        return path, data

    def capic_kube_dom(self):
        vmm_type = "Kubernetes"
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        kube_controller = self.config["kube_config"]["controller"]

        mode = "k8s"
        scope = "kubernetes"

        path = "/api/mo/uni/vmmp-%s/dom-%s.json" % (vmm_type, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "vmmDomP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", vmm_name),
                                        ("mode", mode),
                                        ("enfPref", "sw"),
                                        ("prefEncapMode", "vxlan"),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vmmCtrlrP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    ("name", vmm_name),
                                                                    ("mode", mode),
                                                                    ("scope", scope),
                                                                    ("rootContName", vmm_name),
                                                                    (
                                                                        "hostOrIp",
                                                                        kube_controller,
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def capic_overlay_epg(self, name):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vrfName = "{}-vrf".format(tn_name)
        data = collections.OrderedDict(
            [
                (
                    "cloudEPg",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", name),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        rsToCtx = collections.OrderedDict(
            [
                (
                    "cloudRsCloudEPgCtx",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("tnFvCtxName", vrfName),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        data["cloudEPg"]["children"].append(rsToCtx)
        return data

    def capic_cloudApp(self):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        path = "/api/mo/uni/tn-%s/cloudapp-%s.json" % (tn_name, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "cloudApp",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", vmm_name),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        epg_d = self.capic_overlay_epg("kube-default")
        data["cloudApp"]["children"].append(epg_d)
        epg_s = self.capic_overlay_epg("kube-system")
        data["cloudApp"]["children"].append(epg_s)
        epg_n = self.capic_overlay_epg("kube-nodes")
        data["cloudApp"]["children"].append(epg_n)
        return path, data

    def capic_overlay(self):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        region = self.config["aci_config"]["vrf"]["region"]
        vrfName = "{}-vrf".format(tn_name)
        path = "/api/mo/uni/tn-%s/ctxprofile-%s.json" % (tn_name, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "cloudCtxProfile",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", vmm_name),
                                        ("type", "container-overlay"),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        rsToCtx = collections.OrderedDict(
            [
                (
                    "cloudRsToCtx",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("tnFvCtxName", vrfName),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        regionDn = "uni/clouddomp/provp-aws/region-{}".format(region)
        rsToRegion = collections.OrderedDict(
            [
                (
                    "cloudRsCtxProfileToRegion",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("tDn", regionDn),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )
        pod_subnet = self.config["net_config"]["pod_subnet"]
        cidr = pod_subnet.replace(".1/", ".0/")
        child_list = [rsToRegion, rsToCtx, self.cloudCidr(cidr)]

        for child in child_list:
            data["cloudCtxProfile"]["children"].append(child)

        return path, data

    def cloudCidr(self, cidr):
        cidrMo = collections.OrderedDict(
            [
                (
                    "cloudCidr",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("addr", cidr),
                                        ("primary", "yes"),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        cidrMo["cloudCidr"]["children"].append(self.cloudSubnet(cidr))
        return cidrMo

    def cloudSubnet(self, cidr):
        region = self.config["aci_config"]["vrf"]["region"]
        zone = region + "a"  # FIXME
        subnetMo = collections.OrderedDict(
            [
                (
                    "cloudSubnet",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("ip", cidr),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        subnetMo["cloudSubnet"]["children"].append(self.zoneAttach(region, zone))
        return subnetMo

    def zoneAttach(self, region, zone):
        tDn = "uni/clouddomp/provp-aws/region-{}/zone-{}".format(region, zone)
        zaMo = collections.OrderedDict(
            [
                (
                    "cloudRsZoneAttach",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("tDn", tDn),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )

        return zaMo

    def capic_overlay_dn_query(self):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        ctxProfDN = "uni/tn-%s/ctxprofile-%s" % (tn_name, vmm_name)
        filter = "eq(hcloudCtx.delegateDn, \"{}\")".format(ctxProfDN)
        query = '/api/node/class/hcloudCtx.json?query-target=self&query-target-filter={}'.format(filter)
        return query

    def capic_subnet_dn_query(self):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        pod_gw = self.config["net_config"]["pod_subnet"]
        pod_subnet = pod_gw.replace(".1/", ".0/")
        ctxProfDN = "uni/tn-%s/ctxprofile-%s" % (tn_name, vmm_name)
        subnetDN = "{}/cidr-[{}]/subnet-[{}]".format(ctxProfDN, pod_subnet, pod_subnet)
        filter = "eq(hcloudSubnet.delegateDn, \"{}\")".format(subnetDN)
        query = '/api/node/class/hcloudSubnet.json?query-target=self&query-target-filter={}'.format(filter)
        return query

    def capic_cluster_info(self, overlay_dn):
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_type = "Kubernetes"
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]

        path = "/api/node/mo/comp/prov-%s/ctrlr-[%s]-%s/injcont.json" % (vmm_type, vmm_name, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "vmmInjectedClusterInfo",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", vmm_name),
                                        ("overlayDn", overlay_dn),
                                        ("accountName", tn_name),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )
        return path, data

    def capic_vmm_host(self, hostname, ip, id):
        vmm_type = "Kubernetes"
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]

        path = "/api/node/mo/comp/prov-%s/ctrlr-[%s]-%s/injcont.json" % (vmm_type, vmm_name, vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "vmmInjectedHost",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", "{}.{}".format(vmm_name, hostname)),
                                        ("id", id),
                                        ("mgmtIp", ip),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )
        return path, data

    def hostGen(self):
        return self.capic_vmm_host("node1", "192.168.101.12", "9876543210")

    def capic_kafka_topic(self):
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        path = "/api/node/mo/uni/userext/kafkaext/kafkatopic-%s.json" % (vmm_name)
        data = collections.OrderedDict(
            [
                (
                    "aaaKafkaTopic",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", "{}".format(vmm_name)),
                                        ("partition", "1"),
                                        ("replica", "3"),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )
        return path, data

    def capic_kafka_acl(self, cn):
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        path = "/api/node/mo/uni/userext/kafkaext/kafkaacl-%s.%s.json" % (vmm_name, cn)
        data = collections.OrderedDict(
            [
                (
                    "aaaKafkaAcl",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", "{}.{}".format(vmm_name, cn)),
                                        ("certdn", cn),
                                        ("topic", vmm_name),
                                        ("opr", "0"),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                []
                            ),
                        ]
                    )
                )
            ]
        )
        return path, data

    def nested_dom(self):
        nvmm_type = self.get_nested_domain_type()
        if nvmm_type != "VMware":
            return

        system_id = self.config["aci_config"]["system_id"]
        nvmm_name = self.config["aci_config"]["vmm_domain"]["nested_inside"]["name"]
        nvmm_elag_name = self.config["aci_config"]["vmm_domain"]["nested_inside"]["elag_name"]
        encap_type = self.config["aci_config"]["vmm_domain"]["encap_type"]
        infra_vlan = self.config["net_config"]["infra_vlan"]
        service_vlan = self.config["net_config"]["service_vlan"]

        promMode = "Disabled"
        if encap_type == "vlan":
            promMode = "Enabled"

        nvmm_portgroup = self.config["aci_config"]["vmm_domain"]["nested_inside"]["portgroup"]
        if nvmm_portgroup is None:
            nvmm_portgroup = system_id

        path = "/api/mo/uni/vmmp-%s/dom-%s/usrcustomaggr-%s.json" % (
            nvmm_type,
            nvmm_name,
            nvmm_portgroup,
        )
        data = collections.OrderedDict(
            [
                (
                    "vmmUsrCustomAggr",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", nvmm_portgroup),
                                     ("promMode", promMode)]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvnsEncapBlk",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "from",
                                                                        "vlan-%d"
                                                                        % infra_vlan,
                                                                    ),
                                                                    (
                                                                        "to",
                                                                        "vlan-%d"
                                                                        % infra_vlan,
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvnsEncapBlk",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "from",
                                                                        "vlan-%d"
                                                                        % service_vlan,
                                                                    ),
                                                                    (
                                                                        "to",
                                                                        "vlan-%d"
                                                                        % service_vlan,
                                                                    ),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        if self.use_kubeapi_vlan:
            kubeapi_vlan = self.config["net_config"]["kubeapi_vlan"]
            data["vmmUsrCustomAggr"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "fvnsEncapBlk",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                ("from", "vlan-%d" % kubeapi_vlan),
                                                ("to", "vlan-%d" % kubeapi_vlan),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )
        if encap_type == "vlan":
            vlan_range = self.config["aci_config"]["vmm_domain"]["vlan_range"]
            data["vmmUsrCustomAggr"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "fvnsEncapBlk",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                (
                                                    "from",
                                                    "vlan-%d" % vlan_range["start"],
                                                ),
                                                ("to", "vlan-%d" % vlan_range["end"]),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )
        if nvmm_elag_name:
            nvmm_elag_dn = "uni/vmmp-VMware/dom-%s/vswitchpolcont/enlacplagp-%s" % (
                nvmm_name,
                nvmm_elag_name,
            )
            data["vmmUsrCustomAggr"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "vmmRsUsrAggrLagPolAtt",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                ("status", ""),
                                                ("tDn", nvmm_elag_dn),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )
        self.annotateApicObjects(data)
        return path, data

    def associate_aep(self):
        aep_name = self.config["aci_config"]["aep"]
        phys_name = self.config["aci_config"]["physical_domain"]["domain"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        infra_vlan = self.config["net_config"]["infra_vlan"]
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_type = self.config["aci_config"]["vmm_domain"]["type"]
        system_id = self.config["aci_config"]["system_id"]
        aci_system_id = self.ACI_PREFIX + system_id

        path = "/api/mo/uni/infra.json"
        data = collections.OrderedDict(
            [
                (
                    "infraAttEntityP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict([("name", aep_name)]),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "infraRsDomP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "tDn",
                                                                        "uni/vmmp-%s/dom-%s"
                                                                        % (
                                                                            vmm_type,
                                                                            vmm_name,
                                                                        ),
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "infraRsDomP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "tDn",
                                                                        "uni/phys-%s"
                                                                        % phys_name,
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "infraProvAcc",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "provacc")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "infraRsFuncToEpg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "encap",
                                                                                                    "vlan-%s"
                                                                                                    % str(
                                                                                                        infra_vlan
                                                                                                    ),
                                                                                                ),
                                                                                                (
                                                                                                    "mode",
                                                                                                    "regular",
                                                                                                ),
                                                                                                (
                                                                                                    "tDn",
                                                                                                    "uni/tn-infra/ap-access/epg-default",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "dhcpInfraProvP",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "mode",
                                                                                                    "controller",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        if self.use_kubeapi_vlan:
            kubeapi_vlan = self.config["net_config"]["kubeapi_vlan"]
            if self.config["aci_config"]["use_legacy_kube_naming_convention"]:
                data["infraAttEntityP"]["children"].append(
                    collections.OrderedDict(
                        [
                            (
                                "infraGeneric",
                                collections.OrderedDict(
                                    [
                                        (
                                            "attributes",
                                            collections.OrderedDict([("name", "default")]),
                                        ),
                                        (
                                            "children",
                                            [
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "infraRsFuncToEpg",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "attributes",
                                                                        collections.OrderedDict(
                                                                            [
                                                                                (
                                                                                    "tDn",
                                                                                    "uni/tn-%s/ap-kubernetes/epg-kube-nodes"
                                                                                    % (
                                                                                        tn_name,
                                                                                    ),
                                                                                ),
                                                                                (
                                                                                    "encap",
                                                                                    "vlan-%s"
                                                                                    % (
                                                                                        kubeapi_vlan,
                                                                                    ),
                                                                                ),
                                                                            ]
                                                                        ),
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                )
                                            ],
                                        ),
                                    ]
                                ),
                            )
                        ]
                    )
                )
            else:
                data["infraAttEntityP"]["children"].append(
                    collections.OrderedDict(
                        [
                            (
                                "infraGeneric",
                                collections.OrderedDict(
                                    [
                                        (
                                            "attributes",
                                            collections.OrderedDict([("name", "default")]),
                                        ),
                                        (
                                            "children",
                                            [
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "infraRsFuncToEpg",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "attributes",
                                                                        collections.OrderedDict(
                                                                            [
                                                                                (
                                                                                    "tDn",
                                                                                    "uni/tn-%s/ap-%s/epg-%snodes"
                                                                                    % (
                                                                                        tn_name,
                                                                                        aci_system_id,
                                                                                        self.ACI_PREFIX,
                                                                                    ),
                                                                                ),
                                                                                (
                                                                                    "encap",
                                                                                    "vlan-%s"
                                                                                    % (
                                                                                        kubeapi_vlan,
                                                                                    ),
                                                                                ),
                                                                            ]
                                                                        ),
                                                                    )
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                )
                                            ],
                                        ),
                                    ]
                                ),
                            )
                        ]
                    )
                )

        base = "/api/mo/uni/infra/attentp-%s" % aep_name
        rsvmm = base + "/rsdomP-[uni/vmmp-%s/dom-%s].json" % (vmm_type, vmm_name)
        rsphy = base + "/rsdomP-[uni/phys-%s].json" % phys_name

        if self.associate_aep_to_nested_inside_domain:
            nvmm_name = self.config["aci_config"]["vmm_domain"]["nested_inside"]["name"]
            nvmm_type = self.get_nested_domain_type()
            data["infraAttEntityP"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "infraRsDomP",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                (
                                                    "tDn",
                                                    "uni/vmmp-%s/dom-%s"
                                                    % (nvmm_type, nvmm_name),
                                                )
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )
            rsnvmm = base + "/rsdomP-[uni/vmmp-%s/dom-%s].json" % (nvmm_type, nvmm_name)
            self.annotateApicObjects(data)
            return path, data, rsvmm, rsnvmm, rsphy
        else:
            if self.config["aci_config"]["use_legacy_kube_naming_convention"]:
                rsfun = (
                    base + "/gen-default/rsfuncToEpg-"
                    "[uni/tn-%s/ap-kubernetes/epg-kube-nodes].json" % (tn_name)
                )
            else:
                rsfun = (
                    base + "/gen-default/rsfuncToEpg-"
                    "[uni/tn-%s/ap-%s/epg-%snodes].json" % (tn_name, aci_system_id, aci_prefix)
                )
            self.annotateApicObjects(data)
            return path, data, rsvmm, rsphy, rsfun

    def opflex_cert(self):
        client_cert = self.config["aci_config"]["client_cert"]
        client_ssl = self.config["aci_config"]["client_ssl"]

        path = "/api/mo/uni/infra.json"
        data = collections.OrderedDict(
            [
                (
                    "infraSetPol",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "opflexpAuthenticateClients",
                                            yesno(client_cert),
                                        ),
                                        ("opflexpUseSsl", yesno(client_ssl)),
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )
        self.annotateApicObjects(data)
        return path, data

    def l3out_tn(self):
        system_id = self.config["aci_config"]["system_id"]
        vrf_tenant = self.config["aci_config"]["vrf"]["tenant"]

        path = "/api/mo/uni/tn-%s.json" % vrf_tenant
        data = collections.OrderedDict(
            [
                (
                    "fvTenant",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("name", "%s" % vrf_tenant),
                                        ("dn", "uni/tn-%s" % vrf_tenant),
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-allow-all-filter"
                                                                        % system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "allow-all",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-l3out-allow-all"
                                                                        % system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "allow-all-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "%s-allow-all-filter"
                                                                                                                                % system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )

        flt = "/api/mo/uni/tn-%s/flt-%s-allow-all-filter.json" % (vrf_tenant, system_id)
        brc = "/api/mo/uni/tn-%s/brc-%s-l3out-allow-all.json" % (vrf_tenant, system_id)
        self.annotateApicObjects(data)
        return path, data, flt, brc

    def l3out_contract(self, l3out_instp):
        system_id = self.config["aci_config"]["system_id"]
        vrf_tenant = self.config["aci_config"]["vrf"]["tenant"]
        l3out = self.config["aci_config"]["l3out"]["name"]
        l3out_rsprov_name = "%s-l3out-allow-all" % system_id

        pathc = (vrf_tenant, l3out, l3out_instp)
        path = "/api/mo/uni/tn-%s/out-%s/instP-%s.json" % pathc
        data = collections.OrderedDict(
            [
                (
                    "fvRsProv",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        ("matchT", "AtleastOne"),
                                        ("tnVzBrCPName", l3out_rsprov_name),
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )

        rsprovc = (vrf_tenant, l3out, l3out_instp, l3out_rsprov_name)
        rsprov = "/api/mo/uni/tn-%s/out-%s/instP-%s/rsprov-%s.json" % rsprovc
        self.annotateApicObjects(data)
        return path, data, rsprov

    def kube_user(self):
        name = self.config["aci_config"]["sync_login"]["username"]
        password = self.config["aci_config"]["sync_login"]["password"]

        path = "/api/node/mo/uni/userext/user-%s.json" % name
        data = collections.OrderedDict(
            [
                (
                    "aaaUser",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", name), ("accountStatus", "active")]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "aaaUserDomain",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "all")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "aaaUserRole",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "admin",
                                                                                                ),
                                                                                                (
                                                                                                    "privType",
                                                                                                    "writePriv",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )

        if password is not None:
            data["aaaUser"]["attributes"]["pwd"] = password
        self.annotateApicObjects(data)
        return path, data

    def kube_cert(self):
        name = self.config["aci_config"]["sync_login"]["username"]
        certfile = self.config["aci_config"]["sync_login"]["certfile"]

        if certfile is None:
            return None

        cert = None
        try:
            with open(certfile, "r") as cfile:
                cert = cfile.read()
        except IOError:
            # Ignore error in reading file, it will be logged if/when used
            pass

        path = "/api/node/mo/uni/userext/user-%s.json" % name
        data = collections.OrderedDict(
            [
                (
                    "aaaUser",
                    collections.OrderedDict(
                        [
                            ("attributes", collections.OrderedDict([("name", name)])),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "aaaUserCert",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s.crt" % name,
                                                                    ),
                                                                    ("data", cert),
                                                                ]
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        if cert is None:
            data = None
        if data:
            self.annotateApicObjects(data)
        return path, data

    def isV6(self):
        pod_cidr = self.config["net_config"]["pod_subnet"]
        rtr, mask = pod_cidr.split("/")
        ip = ipaddress.ip_address(rtr)
        if ip.version == 4:
            return False
        else:
            return True

    def editItems(self, config, old_naming):
        items = self.config["aci_config"]["items"]
        if items is None or len(items) == 0:
            err("Error in getting items for flavor")
        for idx in range(len(items)):
            if "consumed" in items[idx].keys():
                cons = items[idx]["consumed"]
                for idx1 in range(len(cons)):
                    if old_naming:
                        cons[idx1] = "kube-" + cons[idx1]
                    else:
                        cons[idx1] = self.ACI_PREFIX + cons[idx1]
                config["aci_config"]["items"][idx]["consumed"] = cons
            if "provided" in items[idx].keys():
                prov = items[idx]["provided"]
                for idx1 in range(len(prov)):
                    if old_naming:
                        prov[idx1] = "kube-" + prov[idx1]
                    else:
                        prov[idx1] = self.ACI_PREFIX + prov[idx1]
                config["aci_config"]["items"][idx]["provided"] = prov

    def kube_tn(self, flavor):
        system_id = self.config["aci_config"]["system_id"]
        tn_name = self.config["aci_config"]["cluster_tenant"]
        pre_existing_tenant = self.config["aci_config"]["use_pre_existing_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        phys_name = self.config["aci_config"]["physical_domain"]["domain"]
        kubeapi_vlan = self.config["net_config"]["kubeapi_vlan"]
        kube_vrf = self.config["aci_config"]["vrf"]["name"]
        kube_l3out = self.config["aci_config"]["l3out"]["name"]
        node_subnet = self.config["net_config"]["node_subnet"]
        pod_subnet = self.config["net_config"]["pod_subnet"]
        kade = self.config["kube_config"].get("allow_kube_api_default_epg") or \
            self.config["kube_config"].get("allow_pods_kube_api_access")
        eade = self.config["kube_config"].get("allow_pods_external_access")
        vmm_type = self.config["aci_config"]["vmm_domain"]["type"]
        v6subnet = self.isV6()

        kube_default_children = [
            collections.OrderedDict(
                [
                    (
                        "fvRsDomAtt",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [
                                            (
                                                "tDn",
                                                "uni/vmmp-%s/dom-%s"
                                                % (vmm_type, vmm_name),
                                            )
                                        ]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsCons",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict([("tnVzBrCPName", "dns")]),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsProv",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("tnVzBrCPName", "health-check")]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsCons",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict([("tnVzBrCPName", "icmp")]),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsCons",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict([("tnVzBrCPName", "istio")]),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsBd",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("tnFvBDName", "kube-pod-bd")]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
        ]

        if kade is True:
            kube_default_children.append(
                collections.OrderedDict(
                    [
                        (
                            "fvRsCons",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnVzBrCPName", "kube-api")]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        if eade is True:
            kube_default_children.append(
                collections.OrderedDict(
                    [
                        (
                            "fvRsCons",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnVzBrCPName",
                                              "%s-l3out-allow-all" % system_id)]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        node_subnet_obj = collections.OrderedDict(
            [
                (
                    "attributes",
                    collections.OrderedDict([("ip", node_subnet), ("scope", "public")]),
                )
            ]
        )

        pod_subnet_obj = collections.OrderedDict(
            [("attributes", collections.OrderedDict([("ip", pod_subnet)]))]
        )
        if eade is True:
            pod_subnet_obj["attributes"]["scope"] = "public"

        if v6subnet:
            ipv6_nd_policy_rs = [
                collections.OrderedDict(
                    [
                        (
                            "fvRsNdPfxPol",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnNdPfxPolName", "kube-nd-ra-policy")]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            ]
            node_subnet_obj["attributes"]["ctrl"] = "nd"
            node_subnet_obj["children"] = ipv6_nd_policy_rs
            pod_subnet_obj["attributes"]["ctrl"] = "nd"
            pod_subnet_obj["children"] = ipv6_nd_policy_rs

        path = "/api/mo/uni/tn-%s.json" % tn_name
        data = collections.OrderedDict(
            [
                (
                    "fvTenant",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", tn_name), ("dn", "uni/tn-%s" % tn_name)]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvAp",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "kubernetes")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-default",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        kube_default_children,
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-system",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "dns",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "icmp",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "health-check",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "icmp",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "kube-api",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-l3out-allow-all"
                                                                                                                                % system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/vmmp-%s/dom-%s"
                                                                                                                                % (
                                                                                                                                    vmm_type,
                                                                                                                                    vmm_name,
                                                                                                                                ),
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsBd",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnFvBDName",
                                                                                                                                "kube-pod-bd",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-nodes",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "dns",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "kube-api",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "icmp",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "health-check",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-l3out-allow-all"
                                                                                                                                % system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "encap",
                                                                                                                                "vlan-%s"
                                                                                                                                % kubeapi_vlan,
                                                                                                                            ),
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/phys-%s"
                                                                                                                                % phys_name,
                                                                                                                            ),
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/vmmp-%s/dom-%s"
                                                                                                                                % (vmm_type, vmm_name),
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsBd",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnFvBDName",
                                                                                                                                "kube-node-bd",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-istio",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "istio",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "kube-api",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "icmp",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "health-check",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "dns"
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/vmmp-%s/dom-%s"
                                                                                                                                % (vmm_type, vmm_name),
                                                                                                                            ),
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsBd",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnFvBDName",
                                                                                                                                "kube-pod-bd",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvBD",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "kube-node-bd",
                                                                    ),
                                                                    (
                                                                        "arpFlood",
                                                                        yesno(True),
                                                                    ),
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvSubnet",
                                                                            node_subnet_obj,
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsCtx",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnFvCtxName",
                                                                                                    kube_vrf,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsBDToOut",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnL3extOutName",
                                                                                                    kube_l3out,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvBD",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "kube-pod-bd",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvSubnet",
                                                                            pod_subnet_obj,
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsCtx",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnFvCtxName",
                                                                                                    kube_vrf,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            )
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsBDToOut",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnL3extOutName",
                                                                                                    kube_l3out,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "icmp-filter",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ipv4",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "icmp",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp6",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ipv6",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "icmpv6",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "health-check-filter-in",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "health-check-filter-out",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "est",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "dns-filter")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-udp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "udp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "kube-api-filter",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-api",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "6443",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "6443",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-api2",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "8443",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "8443",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "istio-filter",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "istio-9080",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "sFromPort",
                                                                                                    "9080",
                                                                                                ),
                                                                                                (
                                                                                                    "sToPort",
                                                                                                    "9080",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "istio-mixer-9091",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "9091",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "9091",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "istio-prometheus-15090",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "sFromPort",
                                                                                                    "15090",
                                                                                                ),
                                                                                                (
                                                                                                    "sToPort",
                                                                                                    "15090",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "istio-pilot-15010",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "15010",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "15010",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "kube-api")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "kube-api-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "kube-api-filter",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "health-check",
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "revFltPorts",
                                                                                                    "yes",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzOutTerm",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "name",
                                                                                                                                "",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                ),
                                                                                                                (
                                                                                                                    "children",
                                                                                                                    [
                                                                                                                        collections.OrderedDict(
                                                                                                                            [
                                                                                                                                (
                                                                                                                                    "vzRsFiltAtt",
                                                                                                                                    collections.OrderedDict(
                                                                                                                                        [
                                                                                                                                            (
                                                                                                                                                "attributes",
                                                                                                                                                collections.OrderedDict(
                                                                                                                                                    [
                                                                                                                                                        (
                                                                                                                                                            "tnVzFilterName",
                                                                                                                                                            "health-check-filter-out",
                                                                                                                                                        )
                                                                                                                                                    ]
                                                                                                                                                ),
                                                                                                                                            )
                                                                                                                                        ]
                                                                                                                                    ),
                                                                                                                                )
                                                                                                                            ]
                                                                                                                        )
                                                                                                                    ],
                                                                                                                ),
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzInTerm",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "name",
                                                                                                                                "",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                ),
                                                                                                                (
                                                                                                                    "children",
                                                                                                                    [
                                                                                                                        collections.OrderedDict(
                                                                                                                            [
                                                                                                                                (
                                                                                                                                    "vzRsFiltAtt",
                                                                                                                                    collections.OrderedDict(
                                                                                                                                        [
                                                                                                                                            (
                                                                                                                                                "attributes",
                                                                                                                                                collections.OrderedDict(
                                                                                                                                                    [
                                                                                                                                                        (
                                                                                                                                                            "tnVzFilterName",
                                                                                                                                                            "health-check-filter-in",
                                                                                                                                                        )
                                                                                                                                                    ]
                                                                                                                                                ),
                                                                                                                                            )
                                                                                                                                        ]
                                                                                                                                    ),
                                                                                                                                )
                                                                                                                            ]
                                                                                                                        )
                                                                                                                    ],
                                                                                                                ),
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "dns")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "dns-filter",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "icmp")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "icmp-filter",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "istio")]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "istio-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "istio-filter",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )

        if eade is not True:
            del data["fvTenant"]["children"][2]["fvBD"]["children"][2]

        if v6subnet is True:
            data["fvTenant"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "ndPfxPol",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                ("ctrl", "on-link,router-address"),
                                                ("lifetime", "2592000"),
                                                ("name", "kube-nd-ra-policy"),
                                                ("prefLifetime", "604800"),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        # If dhcp_relay_label is present, attach the label to the kube-node-bd
        if "dhcp_relay_label" in self.config["aci_config"]:
            dbg("Handle DHCP Relay Label")
            children = data["fvTenant"]["children"]
            dhcp_relay_label = self.config["aci_config"]["dhcp_relay_label"]
            attr = collections.OrderedDict(
                [
                    (
                        "dhcpLbl",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("name", dhcp_relay_label), ("owner", "infra")]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            )
            # lookup kube-node-bd data
            for child in children:
                if "fvBD" in child:
                    if child["fvBD"]["attributes"]["name"] == "kube-node-bd":
                        child["fvBD"]["children"].append(attr)
                        break

        for epg in self.config["aci_config"].get("custom_epgs", []):
            data["fvTenant"]["children"][0]["fvAp"]["children"].append(
                {
                    "fvAEPg": {
                        "attributes": {
                            "name": epg
                        },
                        "children": kube_default_children
                    }
                })

        old_naming = self.config["aci_config"]["use_legacy_kube_naming_convention"]
        if "items" in self.config["aci_config"].keys():
            self.editItems(self.config, old_naming)
            items = self.config["aci_config"]["items"]
            if vmm_type == "OpenShift":
                openshift_flavor_specific_handling(data, items, system_id, old_naming, self.ACI_PREFIX)
            elif flavor == "docker-ucp-3.0":
                dockerucp_flavor_specific_handling(data, items)
        self.annotateApicObjects(data, pre_existing_tenant)
        return path, data

    def cluster_tn(self, flavor):
        system_id = self.config["aci_config"]["system_id"]
        aci_system_id = self.ACI_PREFIX + system_id
        tn_name = self.config["aci_config"]["cluster_tenant"]
        pre_existing_tenant = self.config["aci_config"]["use_pre_existing_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        phys_name = self.config["aci_config"]["physical_domain"]["domain"]
        kubeapi_vlan = self.config["net_config"]["kubeapi_vlan"]
        kube_vrf = self.config["aci_config"]["vrf"]["name"]
        kube_l3out = self.config["aci_config"]["l3out"]["name"]
        node_subnet = self.config["net_config"]["node_subnet"]
        pod_subnet = self.config["net_config"]["pod_subnet"]
        kade = self.config["kube_config"].get("allow_kube_api_default_epg") or \
            self.config["kube_config"].get("allow_pods_kube_api_access")
        eade = self.config["kube_config"].get("allow_pods_external_access")
        vmm_type = self.config["aci_config"]["vmm_domain"]["type"]
        v6subnet = self.isV6()

        kube_default_children = [
            collections.OrderedDict(
                [
                    (
                        "fvRsDomAtt",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [
                                            (
                                                "tDn",
                                                "uni/vmmp-%s/dom-%s"
                                                % (vmm_type, vmm_name),
                                            )
                                        ]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsCons",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict([("tnVzBrCPName", "%s-dns" % aci_system_id)]),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsProv",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("tnVzBrCPName", "%s-health-check" % aci_system_id)]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsCons",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict([("tnVzBrCPName", "%s-icmp" % aci_system_id)]),
                                )
                            ]
                        ),
                    )
                ]
            ),
            collections.OrderedDict(
                [
                    (
                        "fvRsBd",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("tnFvBDName", "%s-pod-bd" % aci_system_id)]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            ),
        ]

        if kade is True:
            kube_default_children.append(
                collections.OrderedDict(
                    [
                        (
                            "fvRsCons",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnVzBrCPName", "%s-api" % aci_system_id)]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        if eade is True:
            kube_default_children.append(
                collections.OrderedDict(
                    [
                        (
                            "fvRsCons",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnVzBrCPName",
                                              "%s-l3out-allow-all" % system_id)]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        node_subnet_obj = collections.OrderedDict(
            [
                (
                    "attributes",
                    collections.OrderedDict([("ip", node_subnet), ("scope", "public")]),
                )
            ]
        )

        pod_subnet_obj = collections.OrderedDict(
            [("attributes", collections.OrderedDict([("ip", pod_subnet)]))]
        )
        if eade is True:
            pod_subnet_obj["attributes"]["scope"] = "public"

        if v6subnet:
            ipv6_nd_policy_rs = [
                collections.OrderedDict(
                    [
                        (
                            "fvRsNdPfxPol",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [("tnNdPfxPolName", "%s-nd-ra-policy" % aci_system_id)]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            ]
            node_subnet_obj["attributes"]["ctrl"] = "nd"
            node_subnet_obj["children"] = ipv6_nd_policy_rs
            pod_subnet_obj["attributes"]["ctrl"] = "nd"
            pod_subnet_obj["children"] = ipv6_nd_policy_rs

        path = "/api/mo/uni/tn-%s.json" % tn_name
        data = collections.OrderedDict(
            [
                (
                    "fvTenant",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", tn_name), ("dn", "uni/tn-%s" % tn_name)]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvAp",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "%s" % aci_system_id)]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%sdefault" % self.ACI_PREFIX,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        kube_default_children,
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%ssystem" % self.ACI_PREFIX,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-dns" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-icmp" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-health-check" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-icmp" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-api" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-l3out-allow-all"
                                                                                                                                % system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/vmmp-%s/dom-%s"
                                                                                                                                % (
                                                                                                                                    vmm_type,
                                                                                                                                    vmm_name,
                                                                                                                                ),
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsBd",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnFvBDName",
                                                                                                                                "%s-pod-bd" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvAEPg",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%snodes" % self.ACI_PREFIX,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-dns" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-api" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsProv",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-icmp" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-health-check" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsCons",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzBrCPName",
                                                                                                                                "%s-l3out-allow-all"
                                                                                                                                % system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "encap",
                                                                                                                                "vlan-%s"
                                                                                                                                % kubeapi_vlan,
                                                                                                                            ),
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/phys-%s"
                                                                                                                                % phys_name,
                                                                                                                            ),
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsDomAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tDn",
                                                                                                                                "uni/vmmp-%s/dom-%s"
                                                                                                                                % (vmm_type, vmm_name),
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "fvRsBd",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnFvBDName",
                                                                                                                                "%s-node-bd" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvBD",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-node-bd" % aci_system_id,
                                                                    ),
                                                                    (
                                                                        "arpFlood",
                                                                        yesno(True),
                                                                    ),
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvSubnet",
                                                                            node_subnet_obj,
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsCtx",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnFvCtxName",
                                                                                                    kube_vrf,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsBDToOut",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnL3extOutName",
                                                                                                    kube_l3out,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "fvBD",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-pod-bd" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvSubnet",
                                                                            pod_subnet_obj,
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsCtx",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnFvCtxName",
                                                                                                    kube_vrf,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            )
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "fvRsBDToOut",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnL3extOutName",
                                                                                                    kube_l3out,
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-icmp-filter" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ipv4",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "icmp",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp6",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ipv6",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "icmpv6",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-health-check-filter-in" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-health-check-filter-out" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "est",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "%s-dns-filter" % aci_system_id)]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-udp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "udp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "dns",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzFilter",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-api-filter" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%sapi" % self.ACI_PREFIX,
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "6443",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "6443",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzEntry",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%sapi2" % self.ACI_PREFIX,
                                                                                                ),
                                                                                                (
                                                                                                    "etherT",
                                                                                                    "ip",
                                                                                                ),
                                                                                                (
                                                                                                    "prot",
                                                                                                    "tcp",
                                                                                                ),
                                                                                                (
                                                                                                    "dFromPort",
                                                                                                    "8443",
                                                                                                ),
                                                                                                (
                                                                                                    "dToPort",
                                                                                                    "8443",
                                                                                                ),
                                                                                                (
                                                                                                    "stateful",
                                                                                                    "no",
                                                                                                ),
                                                                                                (
                                                                                                    "tcpRules",
                                                                                                    "",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                ),
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "%s-api" % aci_system_id)]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "%sapi-subj" % self.ACI_PREFIX,
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "%s-api-filter" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        "%s-health-check" % aci_system_id,
                                                                    )
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "health-check-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "revFltPorts",
                                                                                                    "yes",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzOutTerm",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "name",
                                                                                                                                "",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                ),
                                                                                                                (
                                                                                                                    "children",
                                                                                                                    [
                                                                                                                        collections.OrderedDict(
                                                                                                                            [
                                                                                                                                (
                                                                                                                                    "vzRsFiltAtt",
                                                                                                                                    collections.OrderedDict(
                                                                                                                                        [
                                                                                                                                            (
                                                                                                                                                "attributes",
                                                                                                                                                collections.OrderedDict(
                                                                                                                                                    [
                                                                                                                                                        (
                                                                                                                                                            "tnVzFilterName",
                                                                                                                                                            "%s-health-check-filter-out" % aci_system_id,
                                                                                                                                                        )
                                                                                                                                                    ]
                                                                                                                                                ),
                                                                                                                                            )
                                                                                                                                        ]
                                                                                                                                    ),
                                                                                                                                )
                                                                                                                            ]
                                                                                                                        )
                                                                                                                    ],
                                                                                                                ),
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzInTerm",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "name",
                                                                                                                                "",
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                ),
                                                                                                                (
                                                                                                                    "children",
                                                                                                                    [
                                                                                                                        collections.OrderedDict(
                                                                                                                            [
                                                                                                                                (
                                                                                                                                    "vzRsFiltAtt",
                                                                                                                                    collections.OrderedDict(
                                                                                                                                        [
                                                                                                                                            (
                                                                                                                                                "attributes",
                                                                                                                                                collections.OrderedDict(
                                                                                                                                                    [
                                                                                                                                                        (
                                                                                                                                                            "tnVzFilterName",
                                                                                                                                                            "%s-health-check-filter-in" % aci_system_id,
                                                                                                                                                        )
                                                                                                                                                    ]
                                                                                                                                                ),
                                                                                                                                            )
                                                                                                                                        ]
                                                                                                                                    ),
                                                                                                                                )
                                                                                                                            ]
                                                                                                                        )
                                                                                                                    ],
                                                                                                                ),
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            ),
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "%s-dns" % aci_system_id)]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "dns-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "%s-dns-filter" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzBrCP",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [("name", "%s-icmp" % aci_system_id)]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzSubj",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "name",
                                                                                                    "icmp-subj",
                                                                                                ),
                                                                                                (
                                                                                                    "consMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                                (
                                                                                                    "provMatchT",
                                                                                                    "AtleastOne",
                                                                                                ),
                                                                                            ]
                                                                                        ),
                                                                                    ),
                                                                                    (
                                                                                        "children",
                                                                                        [
                                                                                            collections.OrderedDict(
                                                                                                [
                                                                                                    (
                                                                                                        "vzRsSubjFiltAtt",
                                                                                                        collections.OrderedDict(
                                                                                                            [
                                                                                                                (
                                                                                                                    "attributes",
                                                                                                                    collections.OrderedDict(
                                                                                                                        [
                                                                                                                            (
                                                                                                                                "tnVzFilterName",
                                                                                                                                "%s-icmp-filter" % aci_system_id,
                                                                                                                            )
                                                                                                                        ]
                                                                                                                    ),
                                                                                                                )
                                                                                                            ]
                                                                                                        ),
                                                                                                    )
                                                                                                ]
                                                                                            )
                                                                                        ],
                                                                                    ),
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    ),
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )

        if eade is not True:
            del data["fvTenant"]["children"][2]["fvBD"]["children"][2]

        if v6subnet is True:
            data["fvTenant"]["children"].append(
                collections.OrderedDict(
                    [
                        (
                            "ndPfxPol",
                            collections.OrderedDict(
                                [
                                    (
                                        "attributes",
                                        collections.OrderedDict(
                                            [
                                                ("ctrl", "on-link,router-address"),
                                                ("lifetime", "2592000"),
                                                ("name", "%s-nd-ra-policy" % aci_system_id),
                                                ("prefLifetime", "604800"),
                                            ]
                                        ),
                                    )
                                ]
                            ),
                        )
                    ]
                )
            )

        # If dhcp_relay_label is present, attach the label to the kube-node-bd
        if "dhcp_relay_label" in self.config["aci_config"]:
            dbg("Handle DHCP Relay Label")
            children = data["fvTenant"]["children"]
            dhcp_relay_label = self.config["aci_config"]["dhcp_relay_label"]
            attr = collections.OrderedDict(
                [
                    (
                        "dhcpLbl",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [("name", dhcp_relay_label), ("owner", "infra")]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            )
            # lookup kube-node-bd data
            for child in children:
                if "fvBD" in child:
                    if child["fvBD"]["attributes"]["name"] == ("%s-node-bd" % aci_system_id):
                        child["fvBD"]["children"].append(attr)
                        break

        for epg in self.config["aci_config"].get("custom_epgs", []):
            data["fvTenant"]["children"][0]["fvAp"]["children"].append(
                {
                    "fvAEPg": {
                        "attributes": {
                            "name": epg
                        },
                        "children": kube_default_children
                    }
                })

        old_naming = self.config["aci_config"]["use_legacy_kube_naming_convention"]
        if "items" in self.config["aci_config"].keys():
            self.editItems(self.config, old_naming)
            items = self.config["aci_config"]["items"]
            if vmm_type == "OpenShift":
                openshift_flavor_specific_handling(data, items, aci_system_id, old_naming, self.ACI_PREFIX)
            elif flavor == "docker-ucp-3.0":
                dockerucp_flavor_specific_handling(data, items, system_id)
        self.annotateApicObjects(data, pre_existing_tenant)
        return path, data

    def epg(
        self, name, bd_name, provides=[], consumes=[], phy_domains=[], vmm_domains=[]
    ):
        children = []
        if bd_name:
            children.append(aci_obj("fvRsBd", [('tnFvBDName', bd_name)]))
        for c in consumes:
            children.append(aci_obj("fvRsCons", [('tnVzBrCPName', c)]))
        for p in provides:
            children.append(aci_obj("fvRsProv", [('tnVzBrCPName', p)]))
        for (d, e) in phy_domains:
            children.append(
                aci_obj("fvRsDomAtt", [('encap', "vlan-%s" % e), ('tDn', "uni/phys-%s" % d)]))
        for (t, n) in vmm_domains:
            children.append(aci_obj("fvRsDomAtt", [('tDn', "uni/vmmp-%s/dom-%s" % (t, n))]))
        return aci_obj("fvAEPg", [('name', name), ('_children', children)])

    def bd(self, name, vrf_name, subnets=[], l3outs=[]):
        children = []
        for sn in subnets:
            children.append(aci_obj("fvSubnet", [('ip', sn), ('scope', "public")]))
        if vrf_name:
            children.append(aci_obj("fvRsCtx", [('tnFvCtxName', vrf_name)]))
        for l in l3outs:
            children.append(aci_obj("fvRsBDToOut", [('tnL3extOutName', l)]))
        return aci_obj("fvBD", [('name', name), ('_children', children)])

    def filter(self, name, entries=[]):
        children = []
        for e in entries:
            children.append(aci_obj("vzEntry", e))
        return aci_obj("vzFilter", [('name', name), ('_children', children)])

    def contract(self, name, subjects=[]):
        children = []
        for s in subjects:
            filts = []
            for f in s.get("filters", []):
                filts.append(aci_obj("vzRsSubjFiltAtt", [('tnVzFilterName', f)]))
            subj = aci_obj(
                "vzSubj",
                [('name', s["name"]),
                 ('consMatchT', "AtleastOne"),
                 ('provMatchT', "AtleastOne"),
                 ('_children', filts)],
            )
            children.append(subj)
        return aci_obj("vzBrCP", [('name', name), ('_children', children)])

    def cloudfoundry_tn(self, flavor):
        system_id = self.config["aci_config"]["system_id"]
        tn_name = self.config["aci_config"]["cluster_tenant"]
        vmm_name = self.config["aci_config"]["vmm_domain"]["domain"]
        cf_vrf = self.config["aci_config"]["vrf"]["name"]
        cf_l3out = self.config["aci_config"]["l3out"]["name"]
        node_subnet = [self.config["net_config"]["node_subnet"]]
        pod_subnet = self.config["net_config"]["pod_subnet"]
        vmm_type = self.config["aci_config"]["vmm_domain"]["type"]
        nvmm_name = (
            self.config["aci_config"]["vmm_domain"]["nested_inside"]["name"])
        nvmm_type = self.get_nested_domain_type()
        ap_name = (self.config["cf_config"]["default_endpoint_group"]
                   ["app_profile"])
        app_epg_name = (
            self.config["cf_config"]["default_endpoint_group"]["group"])

        gorouter_contracts = []
        app_epgs = [self.epg(app_epg_name,
                             "cf-app-bd",
                             provides=["gorouter"],
                             consumes=["dns",
                                       "%s-l3out-allow-all" % system_id],
                             vmm_domains=[(vmm_type, vmm_name)])]
        node_epgs = [self.epg(
            self.config["cf_config"]["node_epg"],
            "cf-node-bd",
            provides=["dns", "is-node"],
            consumes=["gorouter", "is-node",
                      "%s-l3out-allow-all" % system_id],
            vmm_domains=[(nvmm_type, nvmm_name)])]

        for iso_seg in self.config["aci_config"].get("isolation_segments", []):
            is_name = iso_seg['name']
            node_subnet.append(iso_seg['subnet'])
            node_epgs.append(
                self.epg(
                    "%s-%s" % (self.config["cf_config"]["node_epg"], is_name),
                    "cf-node-bd",
                    provides=["is-node"],
                    consumes=["gorouter-%s" % is_name,
                              "is-node",
                              "%s-l3out-allow-all" % system_id],
                    vmm_domains=[(nvmm_type, nvmm_name)]))
            app_epgs.append(
                self.epg(
                    is_name,
                    "cf-app-bd",
                    provides=["gorouter-%s" % is_name],
                    consumes=["dns",
                              "%s-l3out-allow-all" % system_id],
                    vmm_domains=[(vmm_type, vmm_name)]))
            gorouter_contracts.append(
                self.contract(
                    'gorouter-%s' % is_name,
                    subjects=[collections.OrderedDict(name='gorouter-subj',
                                                      filters=['tcp-all'])]))

        for epg in self.config["aci_config"].get("custom_epgs", []):
            app_epgs.append(self.epg(
                epg,
                "cf-app-bd",
                provides=["gorouter"],
                consumes=["dns",
                          "%s-l3out-allow-all" % system_id],
                vmm_domains=[(vmm_type, vmm_name)]))

        ap = aci_obj('fvAp',
                     [('name', ap_name),
                      ('_children', node_epgs + app_epgs)])

        app_bd = self.bd('cf-app-bd', cf_vrf,
                         subnets=[pod_subnet],
                         l3outs=[cf_l3out])

        node_bd = self.bd('cf-node-bd', cf_vrf,
                          subnets=node_subnet,
                          l3outs=[cf_l3out])

        tcp_all_filter = self.filter('tcp-all', entries=[
            collections.OrderedDict(
                [('name', 'tcp'),
                 ('etherT', 'ip'),
                 ('prot', 'tcp')])])
        dns_filter = self.filter('dns',
                                 entries=[
                                     collections.OrderedDict(
                                         [('name', 'udp'),
                                          ('etherT', 'ip'),
                                          ('prot', 'udp'),
                                          ('dFromPort', 'dns'),
                                          ('dToPort', 'dns')]),
                                     collections.OrderedDict(
                                         [('name', 'tcp'),
                                          ('etherT', 'ip'),
                                          ('prot', 'tcp'),
                                          ('dFromPort', 'dns'),
                                          ('dToPort', 'dns')])]),
        is_all_filter = self.filter(
            'isolation-segment-all',
            entries=[collections.OrderedDict(name='0')])

        gorouter_contracts.append(self.contract(
            'gorouter',
            subjects=[
                collections.OrderedDict([
                    ('name', 'gorouter-subj'),
                    ('filters', ['tcp-all'])])]))
        dns_contract = self.contract(
            'dns',
            subjects=[
                collections.OrderedDict([
                    ('name', 'dns-subj'),
                    ('filters', ['dns'])])])
        is_node_contract = self.contract(
            'is-node',
            subjects=[
                collections.OrderedDict([
                    ('name', 'is-node-subj'),
                    ('filters', ['isolation-segment-all'])])])
        path = "/api/mo/uni/tn-%s.json" % tn_name
        data = aci_obj('fvTenant',
                       [('name', tn_name),
                        ('dn', "uni/tn-%s" % tn_name),
                        ('_children', [ap, node_bd, app_bd,
                                       tcp_all_filter, dns_filter,
                                       is_all_filter, is_node_contract,
                                       dns_contract] + gorouter_contracts)])
        return path, data


def openshift_flavor_specific_handling(data, items, system_id, old_naming, aci_prefix):
    if items is None or len(items) == 0:
        err("Error in getting items for flavor")
    # kube-systems needs to provide kube-api contract
    if old_naming:
        provide_kube_api_contract_os = collections.OrderedDict(
            [
                (
                    "fvRsProv",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "tnVzBrCPName",
                                            "kube-api",
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )
    else:
        provide_kube_api_contract_os = collections.OrderedDict(
            [
                (
                    "fvRsProv",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "tnVzBrCPName",
                                            "%s-api" % system_id,
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )
    data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(provide_kube_api_contract_os)

    # special case for dns contract
    consume_dns_contract_os = collections.OrderedDict(
        [
            (
                "fvRsCons",
                collections.OrderedDict(
                    [
                        (
                            "attributes",
                            collections.OrderedDict(
                                [
                                    (
                                        "tnVzBrCPName",
                                        "dns",
                                    )
                                ]
                            ),
                        )
                    ]
                ),
            )
        ]
    )
    data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(consume_dns_contract_os)

    # add new contract
    for item in items:
        provide_os_contract = collections.OrderedDict(
            [
                (
                    "fvRsProv",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "tnVzBrCPName",
                                            item['name'],
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )

        consume_os_contract = collections.OrderedDict(
            [
                (
                    "fvRsCons",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "tnVzBrCPName",
                                            item['name'],
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                )
            ]
        )

        if old_naming:
            # 0 = kube-default, 1 = kube-system, 2 = kube-nodes
            if 'kube-default' in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][0]['fvAEPg']['children'].append(consume_os_contract)
            if 'kube-system' in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(consume_os_contract)
            if 'kube-nodes' in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][2]['fvAEPg']['children'].append(consume_os_contract)

            if 'kube-default' in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][0]['fvAEPg']['children'].append(provide_os_contract)
            if 'kube-system' in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(provide_os_contract)
            if 'kube-nodes' in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][2]['fvAEPg']['children'].append(provide_os_contract)

        else:
            # 0 = kube-default, 1 = kube-system, 2 = kube-nodes
            if ('%sdefault' % aci_prefix) in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][0]['fvAEPg']['children'].append(consume_os_contract)
            if ('%ssystem' % aci_prefix) in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(consume_os_contract)
            if ('%snodes' % aci_prefix) in item['consumed']:
                data['fvTenant']['children'][0]['fvAp']['children'][2]['fvAEPg']['children'].append(consume_os_contract)

            if ('%sdefault' % aci_prefix) in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][0]['fvAEPg']['children'].append(provide_os_contract)
            if ('%ssystem' % aci_prefix) in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][1]['fvAEPg']['children'].append(provide_os_contract)
            if ('%snodes' % aci_prefix) in item['provided']:
                data['fvTenant']['children'][0]['fvAp']['children'][2]['fvAEPg']['children'].append(provide_os_contract)

    # add new contract and subject
    for item in items:
        os_contract = collections.OrderedDict(
            [
                (
                    "vzBrCP",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [("name", item['name'])]
                                ),
                            ),
                            (
                                "children",
                                [
                                    collections.OrderedDict(
                                        [
                                            (
                                                "vzSubj",
                                                collections.OrderedDict(
                                                    [
                                                        (
                                                            "attributes",
                                                            collections.OrderedDict(
                                                                [
                                                                    (
                                                                        "name",
                                                                        item['name'] + "-subj",
                                                                    ),
                                                                    (
                                                                        "consMatchT",
                                                                        "AtleastOne",
                                                                    ),
                                                                    (
                                                                        "provMatchT",
                                                                        "AtleastOne",
                                                                    ),
                                                                ]
                                                            ),
                                                        ),
                                                        (
                                                            "children",
                                                            [
                                                                collections.OrderedDict(
                                                                    [
                                                                        (
                                                                            "vzRsSubjFiltAtt",
                                                                            collections.OrderedDict(
                                                                                [
                                                                                    (
                                                                                        "attributes",
                                                                                        collections.OrderedDict(
                                                                                            [
                                                                                                (
                                                                                                    "tnVzFilterName",
                                                                                                    item['name'] + "-filter",
                                                                                                )
                                                                                            ]
                                                                                        ),
                                                                                    )
                                                                                ]
                                                                            ),
                                                                        )
                                                                    ]
                                                                )
                                                            ],
                                                        ),
                                                    ]
                                                ),
                                            )
                                        ]
                                    )
                                ],
                            ),
                        ]
                    ),
                )
            ]
        )
        data['fvTenant']['children'].append(os_contract)

    # add filter and entires to that subject
    for item in items:
        os_filter = collections.OrderedDict(
            [
                (
                    "vzFilter",
                    collections.OrderedDict(
                        [
                            (
                                "attributes",
                                collections.OrderedDict(
                                    [
                                        (
                                            "name",
                                            item['name'] + "-filter",
                                        )
                                    ]
                                ),
                            ),
                            (
                                "children",
                                [],
                            ),
                        ]
                    ),
                )
            ]
        )

        for port in item['range']:
            child = collections.OrderedDict(
                [
                    (
                        "vzEntry",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [
                                            (
                                                "name",
                                                item["name"] + '-' + str(port[0]),
                                            ),
                                            (
                                                "etherT",
                                                item["etherT"],
                                            ),
                                            (
                                                "prot",
                                                item["prot"],
                                            ),
                                            (
                                                "dFromPort",
                                                str(port[0]),
                                            ),
                                            (
                                                "dToPort",
                                                str(port[1]),
                                            ),
                                            (
                                                "stateful",
                                                str(item["stateful"]),
                                            ),
                                            (
                                                "tcpRules",
                                                "",
                                            ),
                                        ]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            )
            os_filter['vzFilter']['children'].append(child)

        data['fvTenant']['children'].append(os_filter)


def dockerucp_flavor_specific_handling(data, ports):

    if ports is None or len(ports) == 0:
        err("Error in getting ports for flavor")
    else:
        for port in ports:
            extra_port = collections.OrderedDict(
                [
                    (
                        "vzEntry",
                        collections.OrderedDict(
                            [
                                (
                                    "attributes",
                                    collections.OrderedDict(
                                        [
                                            (
                                                "name",
                                                port["name"],
                                            ),
                                            (
                                                "etherT",
                                                port["etherT"],
                                            ),
                                            (
                                                "prot",
                                                port["prot"],
                                            ),
                                            (
                                                "dFromPort",
                                                str(port["range"][0]),
                                            ),
                                            (
                                                "dToPort",
                                                str(port["range"][1]),
                                            ),
                                            (
                                                "stateful",
                                                str(port["stateful"]),
                                            ),
                                            (
                                                "tcpRules",
                                                "",
                                            ),
                                        ]
                                    ),
                                )
                            ]
                        ),
                    )
                ]
            )
            data['fvTenant']['children'][7]['vzFilter']['children'].append(extra_port)


if __name__ == "__main__":
    pass
