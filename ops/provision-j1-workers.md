# J1 worker provisioning operation

Operational change: add a worker-only Render Blueprint (`render.worker.yaml`) that provisions only `enigma-j1-worker` with three instances. Existing web and cron services remain outside this bootstrap Blueprint. Production cron must remain in `J1_EXECUTION_MODE=batch` until all three worker instances are confirmed healthy.
