# Current-state one-shot Startup（実装方針）

起点 `ec834d5`。専用ブランチ `feature/state-based-startup`。既存StartupとIPC世代3、Bridge世代1を維持する追加Plan種別。

- `start_missing.py`は現在観測からStart / Skip / Blockと理由を返す純粋関数。Run履歴・承認・永続データを読まない。
- PreviewはIPCで読み取り再観測し、全Stageの予定判断を表示する。直接Python APIのpreviewは現在のキャッシュを使う（必要なら呼出側がpoll）。
- Executeは既存Operatorの一枠を予約して再観測。各StageのInspection後、発行直前にも再観測して必要メンバーだけ共通batchへ渡す。発行・結果・CancelはOperator/Adapterの既存処理。
- 未起動から正常Activeへの変化は要求を減らす。既にActiveだった対象の停止・世代変更、不健全・不明・遷移中はRunを失敗させ、新しいpreviewを要求する。
- Startの結果はジョブから取得。その後の一回の再観測で既存Readiness/Health契約を確認し、満たさなければ後続へ進まない。Health待機・再試行は追加しない。
- 各Stage直前に起動済み前段も再確認する。Checkpoint後にContinueを受ける場合も健全性条件を再確認し、古いtokenを拒否する。正常な新世代を明示再確認した場合はその観測をRun内基準として使う。
- Readiness未提供と取得失敗は区別する。通常systemd Activeは操作省略できるが品質Readyを保証しない。DockerやHTTPなど設定された契約が不明ならBlock。
- 全部Skipでも既存checks境界のCheckpointを残す。空checksでも不健全・不明を通過しない。完了・失敗・中止時にRun内基準を解放する。
- 観測と操作は原子的ではない。直前再確認後にも外部操作は可能。既存ジョブ追跡・世代診断を維持し、強制停止・自動修復はしない。
