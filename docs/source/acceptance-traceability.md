# Acceptance traceability

This matrix maps every design criterion to its owning implementation task and executable evidence.

| Criterion | Task | Automated evidence                                     | Manual evidence                              |
| --------- | ---- | ------------------------------------------------------ | -------------------------------------------- |
| D-AC01    | 07   | `CaptureScreenTest.kt`                                 | Real-device capture                          |
| D-AC02    | 07   | `CaptureWidgetTest.kt`                                 | Widget safeguard                             |
| D-AC03    | 07   | `RecordingStateMachineTest.kt`                         | Offline capture                              |
| D-AC04    | 07   | `CaptureLifecycleTest.kt`                              | Background and reboot                        |
| D-AC05    | 07   | `CaptureLifecycleTest.kt`                              | Storage exhaustion                           |
| D-AC06    | 04   | `api/tests/api/test_uploads.py`                        | Resumable upload                             |
| D-AC07    | 04   | `api/tests/api/test_uploads.py`                        | Network interruption                         |
| D-AC08    | 02   | `api/tests/lifecycle/test_projection.py`               | Client state display                         |
| D-AC09    | 05   | `api/tests/worker/test_grounding.py`                   | English output                               |
| D-AC10    | 05   | `api/tests/worker/test_failures.py`                    | Failure review                               |
| D-AC11    | 02   | `api/tests/lifecycle/test_revisions.py`                | Conflict dialog                              |
| D-AC12    | 08   | `web-ui/tests/admin/permissions.spec.ts`               | Mobile browsing                              |
| D-AC13    | 07   | `CaptureLifecycleTest.kt`                              | Android uncache                              |
| D-AC14    | 02   | `api/tests/lifecycle/test_revisions.py`                | Cross-client metadata                        |
| D-AC15    | 09   | `api/tests/api/test_notes.py`                          | Timestamped notes                            |
| D-AC16    | 04   | `api/tests/lifecycle/test_deletion.py`                 | Delete verification                          |
| D-AC17    | 02   | `api/tests/lifecycle/test_cross_dimension_deletion.py` | No resurrection                              |
| D-AC18    | 08   | `web-ui/tests/admin/permissions.spec.ts`               | Client parity                                |
| D-AC20    | 03   | `api/tests/auth/test_keycloak.py`                      | Device flow                                  |
| D-AC21    | 09   | `api/tests/mcp/test_delegated_session.py`              | Bounded retrieval                            |
| D-AC22    | 09   | `api/tests/mcp/test_delegated_session.py`              | Agent permissions                            |
| D-AC23    | 09   | `api/tests/mcp/test_delegated_session.py`              | OpenCode and Claude                          |
| D-AC24    | 03   | `api/tests/auth/test_contract.py`                      | Ownership isolation                          |
| D-AC25    | 04   | `api/tests/api/test_uploads.py`                        | Expired grants                               |
| D-AC26    | 11   | `api/tests/observability/test_redaction.py`            | Telemetry review                             |
| D-AC27    | 11   | `api/tests/observability/test_health.py`               | Operational diagnostics                      |
| D-AC28    | 11   | `api/tests/recovery/test_cross_store_restore.py`       | Restore exercise                             |
| D-AC29    | 12   | `make qa/api`, `make qa/web`, `make qa/mobile`         | Release checklist                            |
| D-AC30    | 10   | `make qa/e2e`                                          | Clean-cluster install and authenticated flow |
