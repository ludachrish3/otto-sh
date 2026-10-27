# Code-duplication sweep — 2026-10-26

Trigger: #732–#737 work found features re-implemented at several entry points, so a systemic fix landed in one copy and missed the rest. AGENTS.md now carries the "ONE DECISION, ONE PLACE" rule. This file is the backlog the sweep produced. Delete it when every item is consolidated or explicitly ruled out.

## Verified live divergences (fix first)
- [ ] Survey SSH port ignores `ssh_options.extra["port"]`: `survey/engine.py _declared_port_unix` reads `.port`; `SshOptions._kwargs` applies `extra` after it. `extra={"port": 2222}` surveys 22, connects 2222. (Astra #6)
- [ ] LCOV repeated `SF:` records: capture parser (`coverage/capture/model.py` ~149–164) overwrites; report loader (`coverage/merge/lcov_loader.py` ~143–170) accumulates. 3 vs 5 on the same input. (Astra #1)
- [ ] `otto login` bridge via-cred `Cred(login=via)` vs manager `cred_for(...)` — #736.
- [ ] compose `up -d` race retry only in `compose_up`, not `deploy` — #737. Plus: `docker compose -p` built in 7 sites (incl. compose.py:474 remedy string, deployment.py:455 token list); rollback undo sets differ (compose pops placeholders, deploy leaves stale registrations and drops a returned down failure); build-then-raise loop, 3× host stamping, 4× project-name expression, 3× first-line id parsers, 2nd `docker ps` label probe (fix-737 session).
- [ ] DOCKER, handed off from #737 (design §1/§9 in .claude/worktrees/fix-737/.superpowers/sdd/2026-10-26-737/design.md; #737 itself covers the compose builder, up+retry, rollback, teardown, id poll — these are the leftovers, owned here):
  - [ ] build-then-raise loop: compose.py:288-298 vs deployment.py:534 `_build_for`
  - [ ] container-host stamping into the lab ×3: compose.py:425-461, :921-942, :987-1006
  - [ ] `project_name or use_case_project(parent.source_lab, use_case)` ×4: deployment.py:696, :949, :1036, observe.py:394 — cheapest fix is a `use_case_compose_project(parent, use_case, project_name)` helper; it exposes that observe cannot address a `project_name=`-deployed stack (separate gap)
  - [ ] first-line container-id parse ×3: compose.py:208, docker_host.py:395, :455
  - [ ] second label-filter `docker ps` builder: compose.py:171 vs `compose_container_probe` docker_host.py:63
  - [ ] `_project_parent_host` (compose.py:124) is a pure alias of `docker_parent`
- [ ] `_resolve_container_id` attempt-count poll beside `wait_for_async` (compose.py ~204) — and extend `no-handrolled-deadline-poll` with a `for $I in range($N)` + sleep arm.

## Semantic duplicates, identical today (drift risk)
See /tmp/dupsweep/astra_report.md if still present; summary:
- [ ] #2 SSH zombie-transport teardown ×3 (connections.py:1090, :1117, transport.py:352)
- [ ] #3 process terminate→wait→kill escalation ×3 (session.py:3226 `_reap_exec_process`, local_host.py:366, session.py:1625 `LocalSession.close` — restates 5 s, no post-kill wait)
- [ ] #4 SSH auth kwargs envelope ×3 (connections.py:668, :742, remote_host.py:782)
- [ ] #5 privilege undo-chain replay ×2 (privilege.py:394, session.py:1910 `as_user`)
- [ ] #7 container construction+enrichment ×3 (compose.py:425, :921, :987)
- [ ] #8 `docker exec` envelope ×3 (docker_host.py:617, :899, session.py:1697 — quoting and `sh` vs `/bin/sh` differ)
- [ ] #9 tagged-process `ps` scan ×3 (daemon.py:228, tunnel/check_probes.py:35, link/check_live.py:100 — failure marker differs)
- [ ] #10 interface/address selection ×2 (link/derive.py:91, tunnel/manage.py:146 — tunnel rejects empty address)
- [ ] #11 branch-hit merge policy ×2 (coverage/validity.py:56, merge/lcov_loader.py:186)
- [ ] #12 ticket aggregation ×2 (ticket_export.py:133, renderer/spa_data.py:466 — root filter only in export)
- [ ] #13 monitor event UPDATE SQL ×2 (monitor/db.py:466, archive_edit.py:100; INSERT already shared)
- [ ] #14 registry error-redaction ×7 (connections, transfer/registry, power, inventory, creds, labs, reservations)
- [ ] #15 SNMP defaults ×4 + endpoint assembly ×2 (options.py:730, option_specs.py:314, monitor/snmp.py:436, survey/engine.py:280; engine.py:274 vs monitor/factory.py:79)
- [ ] #16 docker default-parent ranking ×2 (docker/observe.py:57, completion_cache.py:1879 — "change both or neither")
- [ ] #17 container declaration/ID formatting ×4 (completion_cache.py:1845/1864, compose.py:968, deployment.py:214, docker_host.py:203)
- [ ] #18 shim workspace identity/home ×2 (config/home.py:117/54 vs _shim_complete.py:148/817) — deliberate import-budget copy; consider generating
- [ ] #19 completion refresh launch env ×2 (completion_cache.py:2383/2465 vs _shim_complete.py:1036/1071)
- [ ] #20 NAWS framing ×2 (telnet.py:247, console.py:188)
- [ ] #21 raw-send policy ×3 (remote_host.py:567, local_host.py:432, docker_host.py:839)
- [ ] #22 JSON comment-key filter ×3 (host/specs.py:606, models/lab.py:36, models/link.py:47)
- [ ] #23 interface shorthand normalization ×2 (host/specs.py:644, models/inventory.py:63)
- [ ] #24 git subprocess env ×3 (scripts/check_breaking_marks.py:149, release_events.py:310, api_regen.py:401 — api_regen strips all GIT_*)
- [ ] #25 docs-capture freshness digest ×2 (capture_docs_media.py:116/129, capture_docs_termynal.py:87/99)
- [ ] #26 capture_docs_media.py:418 deadline poll beside `wait_for`
- [ ] #27 SSH-bed prerequisite ×2 (tests/e2e/host/test_login_proxy_e2e.py:200, test_app_shell_e2e.py:272)
- [ ] `retries_failed_open` static mirror of TelnetSession `_retry_failed_handshake` (agreement test added on fix-x86-lab; still two spellings)

## Left open by #738 (the gates PR)
- [ ] `_resolve_container_id` (compose.py) carries an inline `ast-grep-ignore` until #737 lands its wait_for_async rewrite; drop the suppression with #737.
- [ ] compose_up's libnetwork-race retry and SessionManager's handshake retry are two `for attempt in range(2)` + backoff retries of a one-shot operation, each exempted inline. If a third appears, that is the moment for one `retry_once` primitive in otto.utils beside wait_for.
- [ ] The four e2e migrations onto wait_for/wait_for_async (test_remote_completion_e2e, test_docker_chaos, test_traffic, and the link listener probe's budget) were not run on the bed in #738; one bed pass is owed.
- [ ] scripts/check_duplicate_code.py scans src/otto only; scripts/ and tests/_fixtures are a second scope once the first baseline is burned down.

## Textual clones (pylint R0801, min 6 lines, src/otto) — 24 reports, 14 module pairs
Now the ratchet baseline in scripts/duplicate_code_baseline.txt; each pair removed there is a step.
A clone detector finds these and NONE of the above. Largest: fleet.py:124-193 ↔ run_context.py:1081-1091 and :266-312 ↔ :1147-1180; local_host.py:424-487 ↔ remote_host.py:559-606; options.py ↔ option_specs.py ×4; console.py ↔ telnet.py ×2; attribution.py ↔ capture/treediff.py ×2; creds/json_store.py:80-101 ↔ inventory/json_backend.py:99-121; host/specs.py:662-681 ↔ models/lab.py:114-132; _shim_complete ↔ completion_cache ×2; init/doctor.py:352 ↔ labs/json_repository.py:620; host/connections.py:1089 ↔ transport.py:351; docker_image_kind.py:410 ↔ kmod_kind.py:458; capture/model.py:90 ↔ store/model.py:710; context.py:105 ↔ run_context.py:52.
- [ ] Burn these down, then add `pylint --enable=duplicate-code` as a lint-arch lane so the cheap kind stays at zero.
