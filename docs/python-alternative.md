# Python版の実験・検証記録

> 比較元Coordinator方式（main `d143e97`）の記録です。このStageブランチの現行仕様・検証は[比較レポート](stage-comparison.md)と[設定手順](stage-configuration.md)を参照してください。旧操作の説明をStage版へ適用しないでください。

現在のGUIはライセンス構成を変更するため、Qtからttkbootstrap + Tkinterへ移行しています。詳細は[GUI移行記録](gui-toolkit.md)を参照してください。

## 分離方法

`experiment/python-only`はRust版mainの`35ecdea`から分岐しています。
ローカルでは別worktreeで実装・検証しました。検証後、Python版をmainへ採用する方針に変更しました。
Rust版のコミットは`archive/rust-gpui`ブランチへ保存しています。Python版ではRustアプリ本体・Rust専用テストを除き、Pythonパッケージへ置き換えました。

## 採用した構成

| 用途 | Python版 |
| --- | --- |
| ネイティブGUI | ttkbootstrap 2.2.7 / Tkinter |
| ヘッドレスCore | asyncio、別プロセス |
| systemd | dbus-fast 5.0.22、user/sessionとsystem busを区別 |
| HTTP/Docker | aiohttp、UnixConnector、Engine API 1.41〜1.52 |
| 設定 | PyYAML SafeLoader、重複キー・参照・配置・循環検証 |
| 依存固定 | uv.lock。PySide6/shiboken6は除外 |
| ROS連携 | 既存Python Bridgeを再利用 |

GUIはTreeview/Button/Notebook/Text部品で構成し、ttkbootstrapでテーマを適用しています。
初期のPython試作にはQtを使いましたが、配布時のライセンス構成を変更するためTkinterへ置き換えました。
Core/IPCにGUIライブラリ依存はありません。ランタイムの子要素をGroupのStepsへ入れることもしていません。

## Stepsと責務の境界

Groupを平坦な順序付きStep列へ展開し、Startは正順、Stopは逆順、RestartはStop完了後にStartします。
Parallelだけは並列発行し全結果を待ちます。単一の実行タスクとCancel Eventを使い、複雑な遷移エンジンは追加していません。
Gate待機はキャンセル可能です。ジョブ発行後はCancelJobを送らず、結果を追跡してからCancelledになります。
失敗後のサービス停止・復元、期待状態へのreconciliation、シーケンスの自動再開はありません。

`inspection.py`は取得済み依存情報から診断を返すだけです。接続・取得・更新検知はsystemd Adapter、検査時点の整合性と操作受付はEngineが担当します。
Wants/Requires/BindsTo/Upholdsの起動要求、PartOf等の逆方向停止影響、After/Beforeの順序制約を区別しています。
未承認Gateの迂回、選択Group外への起動/停止、Conflicts、順序差、後続のRequisiteなどを診断します。
強い依存によるGate迂回と選択範囲内のConflictsはエラーとして操作を拒否し、間接的影響は警告にします。

未登録unitも解析に必要な範囲で参照しますが、登録や操作対象には追加しません。
探索は256 unit / 6秒、個々の取得は最大2秒に制限し、欠落・未対応の動的依存を検査未完了として報告します。
設定・承認・manager reload・切断・unit解決変更で診断を無効化し、Group操作直前に再検査します。
systemdトランザクションの完全な予測・模倣は行わず、警告なしでも安全性を保証しません。

## systemdイベントの追跡

- managerの一意なD-Bus ownerに接続世代を固定し、Subscribe前後のownerを確認します。
- JobRemovedを直接購読し、メソッド返信のジョブパスと対応付けます。発行しただけで完了扱いにはしません。
- 返信より先のJobRemovedは最大256件の短い相関待ちとして保持します。溢れた場合は接続世代を失敗にし、監視欠落を明示します。
- 接続喪失・owner変更で世代トークンを失効させ、待機中の操作やGateを中断します。再接続は監視を復旧するだけで、操作を再送しません。
- イベントの受信コールバックではawaitや依存解析をしません。現在状態の取得・診断は別のasyncioタスクです。
- JobRemovedは直接追跡し、登録済みWorkloadの状態は1秒間隔の再解決で確認します。PropertiesChangedを網羅購読する実装ではありません。
- UnitNew/UnitRemovedはロード情報であり、ファイル削除・サービス停止と同一視しません。Not Foundは再解決結果で判定します。
- 初回観測は基準状態です。接続復旧後も基準を取り直し、監視できなかった間の操作履歴を推測しません。
- 自分のジョブパスと一致する場合だけrequestedとします。最近の操作と状態変化の近接は因果関係の証明に使いません。
- GUI向けイベントは直近200件です。Manual Gateと稼働状態の不一致は状態が継続する間、通知を抑制します。

短い遷移がポーリング間で完結する場合は状態差分に残らないことがあります。購読中の完了ジョブは受信できますが、詳細な監査機能ではありません。
監視が欠落した場合は画面へgapを残します。systemd以外の子要素はsnapshotポーリングで、Bridgeのwatchは未使用です。

## 検証結果

初回のPython版は2026-11-07、Ubuntu 24.04 / Python 3.12.3で、34テストを確認しました。
その後Rust版のテストを移植し、118ケースへ拡充しています。最新の対応表と検証は[テスト復元記録](test-restoration.md)を参照してください。

- Core: 階層展開、正逆順、Parallel、部分Restart、承認保持・Reinitialize、個別Gate迂回、boot ID永続化。
- HTTP Gate: ローカルHTTPサーバーによる成功・タイムアウト。
- Cancel/失敗: 新Stepを発行しない、Parallelを追跡し切る、競合拒否、ロールバックなし。
- 診断: Wants/RequiresのGate迂回、After単独は起動要求なし、PartOf/BindsTo/RequiresのGroup外停止、Conflicts、未完了表示。
- D-Busプロトコル: 完了通知の返信前後両方、失敗ジョブ、異なるowner拒否、owner喪失、interactive authorizationと認可エラー。
- 実user manager: 専用ダミーunitの起動・承認・部分Restart・全体Restart・Cancel、別クライアントのStop観測、Reloadによる診断無効化。
- Docker: 専用supervisord fixtureの論理サービス停止/起動、停止サービスのID保持、PID観測専用、Healthと子状態の分離、古いコンテナ世代の操作拒否。
- GUI: Tkinterボタン→Unix Socket→CoreでStart、Gate承認、部分Restart、Reinitialize、Cancel、ログ表示、system/userのScanとImport。BackendはFakeを使用。
- 別Coreプロセス: クライアント切断後のGate維持、承認保存、Core再起動後に操作を再開しない、CLI通信、0700/0600。

user/system双方への実接続とScanも手動確認しました。system unitの実操作やPolkit対話は行っていません。
このブランチでROS実機テストを新しく通したという意味ではありません。再利用Bridgeの既存Jazzyテストと、アプリ接続の追加検証は区別してください。

移行前のQt版の参考計測: GUI用venvは約277 MiB（pytest等の開発依存を含む）。Qtウィンドウをoffscreenで生成し最初のイベントを処理するまで181.3 ms、プロセス最大RSS 92.6 MiBでした。
単発のローカル計測で、Core起動・D-Bus接続・画面全体の描画時間を含むベンチマークではありません。Rust版との性能優劣を示す値にはしていません。

## 現段階の制限・次に比較したいこと

- GUIの詳細・診断はJSON中心です。日本語化、診断表、ログ追尾、選択保持と大量更新の使い勝手は改善余地があります。
- Scan/ImportはGUIテストで確認済みです。状態更新中の全操作組み合わせや大量候補表示は未網羅です。
- 監視状態は1秒ポーリングです。PropertiesChangedの選択購読やBridge watchの利用は後続候補です。
- 通常のROS launch ActionはStopだけです。Start/Restartは安全に成立しないため、既存Bridgeと同じく非対応です。
- システムサービスのPolkit対話、Ubuntu 22.04、Wayland実画面、大規模構成、配布パッケージは未検証です。
- core/GUIの実systemdを通した一体E2E、ROS AdapterとGUI子要素のE2Eを加えると、Rust版との比較の確度が上がります。
- GUI依存のライセンス記録は`gui-toolkit.md`へ移しました。Qtは現在の依存に含めません。

## 調査資料

[Qt for Python](https://doc.qt.io/qtforpython-6/)、[Getting Started](https://doc.qt.io/qtforpython-6/gettingstarted.html)、
[PySide6-Essentials 6.10.3](https://pypi.org/project/PySide6-Essentials/6.10.3/)、
[dbus-fast asyncio API](https://dbus-fast.readthedocs.io/en/latest/message-bus/aio-message-bus.html)、
[systemd D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.systemd1.html)。

## 実装しやすさについての現時点の判断

今の要件ではPythonのほうが素直です。Tkinterの標準ツリー・ボタン・テキスト部品、asyncio、ROSのPython APIをそのまま使えます。
Tkinterのウィジェット操作によるGUIテストも画面座標への依存を避けて書けます。
性能が支配的な用途より、Linuxの既存APIをつなぎ、運用ルールを分かりやすく表示する用途に合っています。

ただしsystemdジョブ相関、manager喪失時の不明結果、依存関係の種類、世代をまたいだ操作拒否といった難しさは同じです。
Pythonだからこれらを省いてよいわけではなく、今回も明示的に実装・テストしました。
Rustは型による静的な保証とPython実行環境を不要にする配布のしやすさに利点があります。
型検査はPython版ではまだruffと実行テスト中心で、mypy/pyrightの導入・全データ構造の型付けは今後の判断です。

この比較を踏まえ、Python版を本流にする方針としました。Rust版は`archive/rust-gpui`で比較・参照用に維持します。
重い計算処理を追加する予定がなく、GUI・ROS・既存Linux APIの統合を保守しやすいことを優先しました。
状態管理の正しさは言語の選択だけでは得られないため、ジョブ追跡・切断・Gate・Cancelのテストを継続します。
