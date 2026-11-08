# 自己管理の防止とコード整理（0.2.1）

> 比較元Coordinator方式（main `d143e97`）の記録です。Stage版の現行仕様とテストは[比較レポート](stage-comparison.md)を参照してください。

Core/GUI自身のsystemd unitを、devdashのWorkloadとして登録・直接操作しないようにしました。
Core自身の起動・停止・再起動は、アプリの外から`systemctl --user`等で行います。
Bridgeはロボット側のWorkloadなので禁止対象に含めません。

## 拒否する対象

- `devdash.service`、`devdash-core.service`、`devdash-gui.service`と、そのインスタンスunit。user/system双方の設定・Importで拒否します。
- 実行中Coreが属するunit。接続時にD-BusのGetUnitByPIDで解決するため、別名やaliasでも判定します。
- system managerから見たCoreの所属unitも保護します。標準的なuser service運用では`user@UID.service`が該当します。
- systemdのExecStartが`devdash`/`devdash-gui`、またはPythonからの`-m devdash`/`-m devdash.gui`等の直接起動を示すunit。停止中の別名unitもImport・操作時に拒否します。

論理IDや表示名だけで判定しません。`devdash-core`という論理IDで通常のcamera.serviceを参照することは許可します。
`devdash-roslaunch`や`python -m devdash_roslaunch`も通常のWorkloadとして扱います。

Scanでは既定名と実行中Coreの所属unit・aliasを候補から除外します。
別名の停止中GUI等がScanに出る場合でも、Import時に実体を確認して拒否します。
一括Importの途中に禁止対象が含まれていても、設定とバックアップは書き換えません。

設定読み込みの静的チェックはunit名を対象にします。別名の実体はCore起動時に解決できれば拒否し、未解決の通常Workloadは従来どおり保持します。
Importと操作直前には再度実体を確認します。登録後のunit定義変更を、過去の検査結果だけで許可しません。

## 整理した責務

- `self_protection.py`: GUI/D-Busに依存しないunit名・起動形式の判定を一箇所に集約。
- `Systemd.Session`: 読み取り用の`unit()`と、管理対象として許可されたか確認する`managed_unit()`を分離。登録と操作で同じ確認を使用。
- `Backends.start()`: 明確な自己参照は起動エラーにし、未検出・通信不能の通常Workloadは消さない。
- `Server.import_units()`: 大きくなったdispatchからImportを分離。解決後のbusy確認・手編集保護・一括保存を維持。
- Backendのイベント通知先はSystemd側へ一本化し、重複した`_emit`状態を削除。
- 呼ばれていなかった`DockerAPI.children()`を削除。
- テスト用D-Bus Managerのunit path生成を共通化。

実行用の依存DAGや新しい管理基盤は追加していません。Stepsの順序、Gate、Cancel、ロールバックしない方針はそのままです。

## 検証

従来の151ケースに38ケースを追加し、全189ケースが成功しました（GUI・専用user unit・Dockerを含む）。
既定名・scope・インスタンス、別名・alias、停止中のExecStart、所属user manager、一般サービス/ROS Bridgeへの誤判定防止、Import拒否時の無変更、実体確認中の競合を検証します。
実user serviceのテストでは、別名で起動したCoreがScanから除外され、Importできないことも確認します。
テスト用Coreを起動・終了する操作はWorkload APIではなく外部のsystemctlへ整理しました。

## 判定の範囲

これは日常操作での自己参照を防ぐガードです。管理者権限に対するセキュリティ境界ではありません。
任意のシェルスクリプトや`python -c`の内容を解析して、停止中unitの実行内容を完全に予測することはしません。
実行中Coreの所属unitはそのようなラッパーでも保護しますが、任意のDocker/Supervisor配置を含めたアプリ自体の発見機能は追加していません。
既存のsystemd依存関係が通常WorkloadからCoreへ停止を伝播する場合など、間接的な副作用を完全に防ぐ機能ではありません。既存の依存検査とunit構成の確認も利用してください。

保護はCore 0.2.1以降で有効です。GUIだけを更新し、古いCoreプロセスを動かし続けた場合には反映されません。
