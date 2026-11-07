# Rust版テストのPython移植と書きやすさ

この文書はテスト復元時（118ケース）の記録です。その後、[常駐運用・互換性](resident-operation.md)と[自己管理防止](self-protection.md)のテストを追加し、現在は自己管理防止も含め189ケースです。

比較元はRust版 `35ecdea`（`archive/rust-gpui`）です。`tests/*.rs`と`src/docker.rs`・`src/systemd_events.rs`内のテスト、合計65本を照合しました。
Pythonでは1本のRustテストを複数の独立テストやパラメーターに分けているため、本数の一致は目標にしていません。
以下は、元の65本が検証していた振る舞いと現在のPythonテストの対応です。別に維持しているROS BridgeのJazzyテストとGUI操作テストは、この65本の集計には含めません。

## 今回戻した保証

- Core: 即時Cancel、Restartの停止中/起動中のCancel、Cancel後の発行済み失敗の保持、進捗の重複防止、HTTP Gate再評価、承認状態の破損検出。
- 依存検査: 未登録unit経由の迂回、停止伝播の方向、親Gateを強制しない部分操作、検査中の変更/承認/reloadによる失効、操作直前の再検査。
- 監視: baseline、観測欠落と復旧、新manager世代への切替、invocation変更、警告抑制、200件のイベント上限、自動修復をしないこと。
- D-Bus: 実際の専用busで完了通知と返信の前後関係、認可拒否、owner交代、bus自体の再起動、初回manager不在、購読完了前の操作拒否、遅いpoll中のイベント、300件のburst、Gate待機中のmanager交換。
- コンテナ: Healthと子状態の分離、停止サービスのID保持、子操作の競合とCancel、APIバージョン、フレーム破損、PIDの監視専用能力、同じCompose参照でのコンテナ再作成。
- 実systemd: 間接依存と設定reload、外部Start/Stop観測、launchを模した子プロセスの終了、登録unitの削除/再追加、user/systemの読み取り。
- IPC: Scanは無変更、Importは明示的、既存unitのみを登録、二重Core起動の拒否、放置socketの再利用、クライアント切断後もGate維持。

## 復元によって見つかった不具合

Python版のImportは、存在しないunitでも論理参照を登録できていました。Rust版のIPCテストを戻して再現し、Import時に対象managerのScan結果へ存在するか確認するよう修正しました。
Scan待ちの間に別の操作が始まる競合もテストし、設定変更前にbusyを再確認します。未知のunitが混ざった一括Importは全体を拒否し、設定・バックアップを変更しません。手編集の上書き拒否も維持します。

また、invocation ID変更で再起動を検出しても、表示が単に`active → active`となり理由が抜けていました。Rust版と同じく`invocation changed`を表示するよう戻し、メッセージも検証します。

今回の本体変更はこのImport確認と観測メッセージだけです。テストを通すためにSteps実行エンジンやDependencyInspectorへ新しい抽象化は追加していません。

## 実際に書きやすさは変わったか

**Core、純粋な検査、監視のテストはPythonのほうが書きやすくなりました。** FakeにTrait実装や`Arc<Mutex<...>>`を用意せず、通常のオブジェクト・リスト・`asyncio.Event`で入力と観測点を表現できます。
同じ契約のバリエーションを`pytest.mark.parametrize`で列挙でき、Fakeの必要なメソッドだけをテスト内で差し替えられます。
例えばCancel後の失敗は「2ジョブが入るまで待つ→Cancel→Eventを解放→片方Failed/片方Completed/残りSkipped」をそのまま記述できます。

**本物のD-Busの障害テストは、準備が多少短くなった程度です。** 名前の所有者、購読、ジョブパス、切断、再接続を再現する専用fixtureは引き続き必要です。
`test_protocol.py`のメソッド差し替えだけで済ませず、`private_bus.py`で本当のdbus-daemonを起動し、実メッセージで検証しています。
専用busの設定にはservice directoryを置かず、ホストのbusアドレスを変更しません。停止・再開の順番はEventで制御し、固定sleepで競合を当てにしません。

単純な行数の参考では、Rustの`tests/events.rs`は658行、Pythonの対応テストと専用bus fixtureは約470行です。Python側にはsystem/user同名unitと認可テストも含みます。
設定やデータ構造の形はRustほどコンパイラに守られないため、Pythonではテスト実行時のエラーを見落とさないことがより大切です。
テストfixture自身の非同期Taskの例外も回収し、裏で失敗したままテストだけ成功することを避けています。

GUIはTkinterウィジェットの選択・Button.invokeを使い、CoreまでUnix Socketで接続する形です。以前のGPUIの画面座標に依存するテストより扱いやすく、QtTestと比べるとイベントループを自分で進める手間は残ります。
GUIテストではBackendをFakeにしており、実systemdまで通したGUI一体E2Eを保証しているわけではありません。

## 検証結果

Ubuntu 24.04 / Python 3.12.3で118ケースを確認しています。通常のヘッドレス113ケース、Tkinter GUI 1ケース、実systemd 3ケース、専用Docker 1ケースです。
単に65本を数で置き換えたのではなく、対応表の各保証を、現在の実装に合うテスト境界で検証しています。
診断コードの`order_mismatch`→`ordering`や、操作前検査の失敗を`run.error`へ格納する違いは、Python側の表示仕様に合わせています。

## 実行

```bash
# dbus-daemonが必要。GUI・実systemd・Dockerへの依存は不要
uv run pytest -q tests/python -m 'not gui and not integration'

# 全件（ディスプレイ、実user manager、専用Docker fixtureが必要）
DEVDASH_GUI_TEST=1 DEVDASH_SYSTEMD_TEST=1 DEVDASH_DOCKER_TEST=1 uv run pytest -q tests/python
```

実systemdの操作対象は一意な`devdash-py-it-*` runtime user unitだけです。system managerは読み取りのみ、Dockerも一意な専用fixtureだけを操作・削除します。
ROS 2 Jazzyの独立Bridgeは今回変更していないため、任意の実ROSテストを新たに実行した集計にはしていません。

## 対応表（65本）

### [src/docker.rs](https://github.com/Ryo45/devdash/blob/35ecdea/src/docker.rs) — 3件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `health_fallback_does_not_aggregate_children` | [test_health_and_docker_frames](../tests/python/test_docker.py#L14) / [test_stopped_service_identity_and_container_health_are_separate](../tests/python/test_container_model.py#L148) |
| `api_range_and_multiplexed_errors` | [test_api_negotiation_rejects_unsupported_server](../tests/python/test_container_model.py#L124) / [test_multiplexed_errors](../tests/python/test_container_model.py#L142) |
| `top_has_observation_only_capabilities_and_stopped_services_keep_identity` | [test_top_resources_are_observation_only](../tests/python/test_container_model.py#L176) / [test_stopped_service_identity_and_container_health_are_separate](../tests/python/test_container_model.py#L148) |

### [src/systemd_events.rs](https://github.com/Ryo45/devdash/blob/35ecdea/src/systemd_events.rs) — 3件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `reply_and_completion_order_do_not_change_attribution` | [test_job_completion_before_and_after_reply](../tests/python/test_protocol.py#L42) |
| `unrelated_jobs_during_pending_method_are_flushed_after_reply` | [test_unrelated_jobs_during_pending_method_are_flushed_after_reply](../tests/python/test_protocol.py#L127) |
| `overflow_marks_tracking_unknown_instead_of_clearing_owned_jobs_silently` | [test_overflow_marks_tracking_unknown_without_silently_losing_owned_jobs](../tests/python/test_protocol.py#L150) |

### [tests/authorization.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/authorization.rs) — 1件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `system_authorization_uses_polkit_flags_and_reports_denial_without_retries` | [test_system_authorization_and_same_named_units_use_separate_managers](../tests/python/test_events.py#L38) |

### [tests/container_model.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/container_model.rs) — 4件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `container_reference_validation_and_children_cannot_be_steps` | [test_container_reference_validation_and_children_cannot_be_steps](../tests/python/test_container_model.py#L38) |
| `container_gate_delegates_health_and_is_independent_of_systemd_manager` | [test_container_gate_is_independent_of_systemd_and_delegates_health](../tests/python/test_container_model.py#L45) |
| `child_operation_uses_global_busy_and_cancel_tracks_issued_operation` | [test_child_operation_uses_global_busy_and_cancel_drains_issued_operation](../tests/python/test_container_model.py#L77) |
| `missing_docker_socket_reports_access_context` | [test_missing_docker_socket_reports_access_context](../tests/python/test_container_model.py#L103) |

### [tests/core.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/core.rs) — 19件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `hierarchy_order_reverse_restart_and_scope` | [test_forward_reverse_and_partial_restart](../tests/python/test_core.py#L12) / [test_stop_skips_gate_and_reverses_hierarchy](../tests/python/test_core_regressions.py#L210) |
| `manual_wait_approve_and_direct_bypass` | [test_manual_gate_individual_bypass_and_reinitialize](../tests/python/test_core.py#L30) |
| `cancel_gate_and_skip_following_no_rollback` | [test_cancel_gate_and_skip_following_no_rollback](../tests/python/test_core_regressions.py#L219) |
| `parallel_cancel_drains_issued_jobs` | [test_parallel_cancel_drains_issued_jobs](../tests/python/test_core_regressions.py#L231) |
| `restart_cancel_during_stop_never_starts` | [test_restart_cancel_during_stop_never_starts](../tests/python/test_core_regressions.py#L34) |
| `failure_drains_parallel_and_no_rollback` | [test_parallel_failure_drains_and_no_rollback](../tests/python/test_core.py#L67) |
| `persistence_boot_reset_and_corruption` | [test_boot_persistence](../tests/python/test_core.py#L91) / [test_persisted_approval_corruption_is_not_silently_discarded](../tests/python/test_core_regressions.py#L143) |
| `rejects_invalid_references_cycles_duplicates_and_steps` | [test_validation](../tests/python/test_core.py#L103) / [test_unknown_references_and_multiple_step_kinds](../tests/python/test_core_regressions.py#L151) |
| `http_success_and_reevaluation` | [test_http_success_and_reevaluation](../tests/python/test_core_regressions.py#L187) |
| `http_timeout` | [test_http_gate](../tests/python/test_core.py#L136) |
| `http_cancel` | [test_http_cancel](../tests/python/test_core_regressions.py#L200) |
| `direct_restart_uses_backend_restart` | [test_direct_restart_uses_backend_restart](../tests/python/test_core_regressions.py#L27) |
| `immediate_cancel_does_not_issue_individual_operation` | [test_immediate_cancel_does_not_issue_individual_operation](../tests/python/test_core_regressions.py#L18) |
| `stop_failure_prevents_restart_start_phase` | [test_stop_failure_prevents_restart_start_phase](../tests/python/test_core_regressions.py#L45) |
| `reinitialize_resets_only_selected_group_gates` | [test_reinitialize_resets_only_selected_group_gates](../tests/python/test_core_regressions.py#L54) |
| `system_scope_operations_reach_backend` | [test_system_scope_operations_reach_backend](../tests/python/test_core_regressions.py#L68) |
| `restart_cancel_in_start_phase_skips_remainder` | [test_restart_cancel_in_start_phase_skips_remainder](../tests/python/test_core_regressions.py#L85) |
| `progress_tracks_steps_and_parallel_members_once` | [test_progress_tracks_steps_and_parallel_members_once](../tests/python/test_core_regressions.py#L108) |
| `issued_job_failure_is_preserved_after_cancel` | [test_issued_job_failure_is_preserved_after_cancel](../tests/python/test_core_regressions.py#L123) |

### [tests/docker.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/docker.rs) — 1件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `supervisor_services_health_epochs_and_container_recreation` | [test_supervisor_services_health_epochs_and_container_recreation](../tests/python/test_docker.py#L28) |

### [tests/events.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/events.rs) — 11件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `delayed_reply_is_correlated_without_false_external_warning` | [test_delayed_reply_is_correlated_without_false_external_warning](../tests/python/test_events.py#L22) |
| `manager_owner_loss_finishes_pending_operation_and_releases_busy` | [test_manager_owner_loss_finishes_pending_operation_and_releases_busy](../tests/python/test_events.py#L52) |
| `burst_of_more_than_64_events_does_not_stall_control` | [test_burst_of_more_than_64_events_does_not_stall_control](../tests/python/test_events.py#L69) |
| `events_during_initial_baseline_are_buffered` | [test_events_during_initial_baseline_are_buffered](../tests/python/test_events.py#L79) |
| `slow_poll_does_not_delay_event_drain_and_reload_warns_during_operation` | [test_slow_poll_does_not_delay_events_and_reload_warns_during_operation](../tests/python/test_events.py#L97) |
| `replacement_owner_resubscribes_before_accepting_new_work_and_rebaselines` | [test_replacement_owner_resubscribes_before_accepting_new_work_and_rebaselines](../tests/python/test_events.py#L126) |
| `disconnected_bus_is_reopened_at_same_address` | [test_disconnected_bus_is_reopened_at_same_address_without_replay](../tests/python/test_events.py#L156) |
| `managers_absent_at_startup_recover_without_restarting_core` | [test_managers_absent_at_startup_recover_without_restarting_core](../tests/python/test_events.py#L176) |
| `group_waiting_at_gate_is_not_resumed_after_manager_replacement` | [test_group_waiting_at_gate_is_not_resumed_after_manager_replacement](../tests/python/test_events.py#L189) |
| `repeated_registered_unit_metadata_signals_do_not_displace_jobs` | [test_repeated_registered_unit_metadata_signals_do_not_displace_jobs](../tests/python/test_events.py#L203) |
| `manager_loss_during_operation_preparation_releases_busy_without_starting` | [test_manager_loss_during_operation_preparation_does_not_start](../tests/python/test_events.py#L213) |

### [tests/inspection.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/inspection.rs) — 19件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `wants_and_requires_bypass_gate_with_different_certainty` | [test_gate_bypass](../tests/python/test_inspection.py#L18) / [test_unregistered_intermediate_can_bypass_gate](../tests/python/test_inspection.py#L51) |
| `unregistered_intermediate_and_inverse_stop_propagation` | [test_unregistered_intermediate_can_bypass_gate](../tests/python/test_inspection.py#L51) / [test_stop_propagation_is_directional](../tests/python/test_inspection.py#L61) |
| `ordering_does_not_create_start_requests` | [test_after_is_only_ordering](../tests/python/test_inspection.py#L26) |
| `before_conflicts_and_stop_to_are_distinct` | [test_before_conflicts_and_stop_to_are_distinct](../tests/python/test_inspection.py#L76) |
| `approval_reuse_and_reinitialize_change_gate_analysis` | [test_approval_reuse_and_reinitialize_change_gate_analysis](../tests/python/test_inspection.py#L100) |
| `same_name_different_managers_and_import` | [test_same_name_different_managers_and_import](../tests/python/test_inspection.py#L108) |
| `partial_inspection_is_explicit_and_cycles_terminate` | [test_partial_inspection_is_explicit_and_cycles_terminate](../tests/python/test_inspection.py#L126) |
| `configuration_is_reinspected_before_every_operation_and_blocked_before_jobs` | [test_reinspected_before_every_operation_and_blocked_before_jobs](../tests/python/test_preflight.py#L11) |
| `observation_baseline_unattributed_changes_and_no_repeat_notifications` | [test_observation_baseline_unattributed_changes_and_no_repeat_notifications](../tests/python/test_monitor.py#L10) |
| `gate_mismatch_and_unexpected_change_during_operation_warn_once` | [test_gate_mismatch_and_unexpected_change_during_operation_warn_once](../tests/python/test_monitor.py#L23) |
| `invocation_restart_and_bounded_log` | [test_invocation_restart_and_bounded_log](../tests/python/test_monitor.py#L37) |
| `cancel_during_preflight_preserves_approvals_and_issues_no_jobs` | [test_cancel_during_preflight_preserves_approvals_and_issues_no_jobs](../tests/python/test_preflight.py#L40) |
| `partial_group_warns_about_outside_start_without_enforcing_parent_gate` | [test_partial_group_warns_without_enforcing_parent_gate](../tests/python/test_inspection.py#L137) |
| `observation_outage_does_not_clear_readiness_episode_or_infer_restart` | [test_observation_outage_does_not_clear_readiness_episode_or_infer_restart](../tests/python/test_monitor.py#L51) |
| `invalidated_refresh_cannot_publish_old_snapshot_as_fresh` | [test_invalidated_refresh_cannot_publish_old_snapshot_as_fresh](../tests/python/test_preflight.py#L53) |
| `approval_changes_invalidate_cached_inspection` | [test_approval_changes_invalidate_cached_inspection](../tests/python/test_preflight.py#L75) |
| `operation_inspection_seeds_only_selected_scope` | [test_operation_inspection_seeds_only_selected_scope](../tests/python/test_preflight.py#L82) |
| `changed_preflight_is_refused_before_jobs` | [test_changed_preflight_is_refused_before_jobs](../tests/python/test_preflight.py#L97) |
| `manager_generation_change_does_not_carry_old_invocation_into_new_baseline` | [test_manager_generation_change_does_not_carry_old_invocation_into_new_baseline](../tests/python/test_monitor.py#L64) |

### [tests/ipc.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/ipc.rs) — 1件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `clients_disconnect_without_losing_gate_and_import_is_explicit` | [test_clients_disconnect_without_losing_gate_and_import_is_explicit](../tests/python/test_import.py#L34) / [test_real_daemon_disconnect_restart_persistence_and_private_socket](../tests/python/test_ipc.py#L10) |

### [tests/systemd.rs](https://github.com/Ryo45/devdash/blob/35ecdea/tests/systemd.rs) — 3件

| Rustのテスト | Pythonで保証するテスト |
| --- | --- |
| `robot_real_systemd` | [test_real_user_jobs_gates_partial_restart_cancel_and_external_events](../tests/python/test_systemd.py#L24) |
| `dependencies_events_and_launch_children` | [test_dependencies_events_and_launch_children](../tests/python/test_systemd.py#L125) |
| `both_managers_readonly` | [test_both_managers_readonly](../tests/python/test_systemd.py#L251) |
