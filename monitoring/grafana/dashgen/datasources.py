"""
Datasource UID constants.

Grafana auto-generates UIDs the first time a datasource is provisioned. The
UIDs below are what the running Grafana instance on Jupiter currently has for
the provisioned Prometheus + Loki datasources. If you ever wipe Grafana's
state volume, these UIDs will regenerate and you'll need to update them here
(or pin them explicitly in monitoring/grafana/provisioning/datasources/datasources.yml).

Verify current UIDs with:
    curl -s http://10.0.0.248:3000/api/datasources -u admin:<pw> | jq '.[] | {name,uid,type}'
"""

PROMETHEUS_UID = "PBFA97CFB590B2093"
LOKI_UID = "P8E80F9AEF21F6940"

PROMETHEUS = {"type": "prometheus", "uid": PROMETHEUS_UID}
LOKI = {"type": "loki", "uid": LOKI_UID}
