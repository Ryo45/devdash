# Stage設定の編集・生成・移行・適用

編集元とRuntimeは同じ検証モデル（Workloads / Stages / dependencies）を使います。別形式の互換Coordinatorや汎用プラグインはありません。`model.py`はGUIやsystemd接続に依存しません。

On-demand Plan版でもこの形式とGeneratorを維持します。Generatorは任意であり、生成世代を持たないYAMLでもOperator/Planを利用できます。任意の`plan_templates`は静的定義として保存・生成時に保持します（例：`boot: {kind: startup}`）。TemplateはYAMLで編集します。EditorのStage `checks`は単発操作では確認事項の表示、Planでは最終Stage以外のStart後のCheckpointに使います。空ならCheckpointはありません。Stage版の既存YAMLはそのまま利用でき、Plan用に`checks`をレビューするだけです。

## 編集モデル

Workloadは既存unitまたはコンテナの参照です。既存のLifecycle / Observer / Child Controller設定を維持します。ExecStart、Restart、環境変数、WorkingDirectory等は既存のGit管理unitに残します。

Stageは順序付きリストで、ID、表示名、説明、確認事項、Workload一覧を持ちます。Workloadは最大一つのStageに属し、未割り当ては個別操作・Import作業用として許可します。必須依存の両端は割り当て済み、同一managerのsystemd Workload、前段→後段である必要があります。

GUIではフォームをApplyしてモデルに反映し、Validateで検証します。Stageの並び替えも依存条件に違反すれば拒否します。複数箇所の大きな変更は、依存を一度外して再編集するか、Git上のYAMLを外部エディターで編集してください。複雑なunit本体はGUIで編集しません。

## 出力・差分・原子的な公開

`devdash generate SOURCE OUTPUT`は読み取り専用プレビューです。`--write`を付けると次の構造を作ります。

```text
OUTPUT/
  .devdash-generated/
    OWNER
    .lock                   # Gitには含めない
    <content hash>/
      .manifest.json        # 所有ファイル名と内容hash
      devdash.yaml
      systemd/user/detector.service.d/60-devdash-stage-dependencies.conf
      ...
  current -> .devdash-generated/<content hash>
```

同じ入力から同じ内容と世代hashを生成します。Stage順序だけから依存は生成しません。明示`dependencies`だけが、追加的なRequisite / Afterになります。既存unitや他のdrop-inをリセットする空の`Requisite=`などは出力しません。

生成は一時ディレクトリへ全出力を書き、実行YAMLを検証後に世代を確定し、最後に`current`を原子的に置き換えます。途中失敗しても以前の`current`は有効です。未参照世代が残る場合はありますが、Runtimeはそれを読みません。電源断を含むファイルシステム全体のトランザクションを提供する仕組みではありません。

前世代との追加・変更・削除・無変更をプレビューします。不要になったdrop-inは新しいcurrentから外れ、古い世代自体は比較・復元用として残します。Source ImportやSaveを生成Runtimeへ直接行うことも拒否します。生成領域内の手編集、未知の追加ファイル、manifest破損、symlinkの混入、無関係なcurrent/storeへの衝突は拒否します。所有領域外のファイルを削除しません。sourceの保存は読み込んだ元テキストとの一致を確認し、古い編集内容で既存ファイルを上書きしません。

Gitでは編集元、生成世代のmanifest・内容、current symlinkを管理できます。[生成済みサンプル](../examples/generated-robot/)を含めています。`.lock`と一時ファイルは除外します。古い世代の削除は自動で行いません。

## 実ホストへの適用は別操作

Generatorはsystemdの検索パスへ書き込まず、daemon-reload、Start、Stop、Restartを呼びません。

1. Sourceを編集・保存し、Generateで差分を確認します。必要なら`--inspect`またはGUIのライブInspectionで既存unitの依存を確認します。
2. 出力のscope（user/system）を確認し、生成した**所有drop-inだけ**を実ホストへ手動配置します。unit本体や別名のdrop-inを上書きしません。
3. 差分のremove対象は、以前に配置した同じ所有ファイルだけを手動で除去します。新しいbundleをコピーするだけでは、ホスト上の古いdrop-inは消えません。
4. 利用者が該当managerにdaemon-reloadを明示実行します。進行中の操作や他サービスへの影響も利用者が確認します。
5. Coreの`--config`は`OUTPUT/current/devdash.yaml`を指定し、GUIのReload Core configまたは`devdash reload`で明示再読込します。操作中は拒否されます。保存・生成だけでは反映しません。
6. 新しい観測値を確認して、必要なStageだけStartします。

例としてuserの単一ファイルを適用する場合の形です。unit名・パスは自分の構成に置き換えてください。devdashはこれらを実行しません。

```bash
mkdir -p "$HOME/.config/systemd/user/detector.service.d"
cp generated/current/systemd/user/detector.service.d/60-devdash-stage-dependencies.conf \
  "$HOME/.config/systemd/user/detector.service.d/60-devdash-stage-dependencies.conf"
systemctl --user daemon-reload
# Coreを生成されたcurrentへ向けた状態で
devdash reload
```

systemの配置先と認可は別です。別manager間の依存は生成できません。GUI/Coreをrootにする運用は不要です。

既存のWants/Requires/PartOf/BindsTo/Conflicts/After等はそのまま残るため、生成範囲外の依存もInspectionで診断します。Editorでのライブ検査は、生成前なので新しいRequisite/Afterが未適用であること自体はエラーにしません。RuntimeのStage操作時には、宣言されたRequisite/Afterの未適用をエラーにします。動的トリガー、解析予算、未登録unit、既存の複雑なトランザクションを完全に再現するものではありません。個別Workload操作は従来どおりBackendへ直接要求するデバッグ操作で、Stage Inspectionによる拒否を経由しません。実際に適用されたsystemd依存と自己管理防止・認可は有効で、YAML上の依存宣言だけが個別操作を強制制約するわけではありません。

## Rollback

過去の正常なSourceをGitから取得し、別の出力ディレクトリへ再生成・差分確認します。以前のdrop-in内容と削除対象を手動復元し、managerをdaemon-reloadしてからCoreをそのcurrentへ再読込します。単にYAMLだけ旧版へ戻してもsystemd側の設定は戻りません。devdashはサービスを自動停止・再起動して整合させません。

## Scan / Import

GUIのScanは既存サービス候補を選び、Workloadフォームへ反映するだけです。Apply → Save → Generate → review/apply → Reloadの順で利用します。CLIは`source-import SOURCE scope units...`で編集元へ反映します。Scan結果の自動登録、生成Runtimeの直接変更、unitのExecStart複製は行いません。

## 旧設定の移行

旧YAMLは新Coreに直接読み込ませず、`devdash migrate OLD`で候補と警告を確認します。旧Workload参照は保持しますが、旧順序だけで必須依存を推測しません。

- Parallelは一つのStage、逐次Workload Stepは個別Stageにする保守的な候補です。
- 階層Groupはソース順で展開します。複数rootはソースの表示順です。Group境界とStage名をレビューして整理してください。
- Manual Gateは確認事項になります。承認状態、boot ID、途中シーケンスは移行しません。
- HTTP/container_ready条件はレポートに原文で残します。Ready委譲が必要な条件がある場合、`--ack-readiness-delegation`なしでは新設定を保存しません。未使用のReady定義もレポートします。
- 循環、重複所属、不明参照、不正なStepはエラーです。変換不可能なものを無視して実行しません。
- 出力は新規パス限定で、`.migration.json`レポートも保存します。旧ファイルや既存出力を上書きしません。

```bash
devdash migrate tests/fixtures/legacy/robot.yaml
devdash migrate tests/fixtures/legacy/robot.yaml --output robot-stage-source.yaml
devdash edit robot-stage-source.yaml
```

HTTPの観測は`observer.http`へ明示設定できます。観測は必須Readyの代わりにはなりません。起動条件はType=notifyや、unitのExecStartPost等で下位へ委譲してください。Docker向けは[既存の有限Health待機PoC](../examples/systemd-docker-health/)をそのまま再利用します。
