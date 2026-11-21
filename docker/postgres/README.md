# OpenOntology PostgreSQL 16 Production Backend

Standalone local PostgreSQL 16 container (`open-ontology-pg`) bound strictly to localhost port `127.0.0.1:15432`, isolated from existing containers.

## Quick Start

```bash
docker compose -f docker/postgres/docker-compose.yml up -d --wait
```

Default DSN:

```text
postgresql://open_ontology:open_ontology_dev@127.0.0.1:15432/open_ontology
```

Override password via `OPEN_ONTOLOGY_PG_PASS`.
