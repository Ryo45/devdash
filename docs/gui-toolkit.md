# GUIをttkbootstrap + Tkinterへ移行

ライセンス構成を変更するため、PySide6 / Qt Widgetsをttkbootstrap 2.2.7 + Tkinterへ置き換えました。
Core、systemd/Docker Adapter、Steps実行、IPCのプロトコルやGate永続化は変更していません。
Qt版はコミット`3fa3218`および`experiment/python-only`で参照できます。Rust版は`archive/rust-gpui`です。

## 実装

- ttkbootstrap 2.2.7を固定し、現行の`bootstrap-dark`テーマを使用。
- TreeviewにGroup/Workload/子サービスとsystem/userの区別を表示。
- Button、Notebook、Textで操作・承認・進捗・診断・イベント・ログを表示。
- IPCは既存のThreadPoolExecutorへ委譲。Futureの結果だけをTkの`after`コールバックで反映し、ワーカーからTkを呼ばない。
- ツリーの項目IDに論理IDと世代を使い、スクロール・展開・選択を更新時に維持。子要素の世代変更時は選択を解除。
- Scan / Importは非モーダル。manager切替時に候補と選択を消し、古いScan応答を世代番号で破棄。
- GUIを閉じてもCoreへ停止・キャンセルを送らない。

利用方法とCLIコマンドは従来どおりです。

```bash
sudo apt-get install -y python3-tk
uv sync --locked --extra gui --group dev --python /usr/bin/python3
uv run devdash daemon --config examples/robot.yaml
# 別ターミナル
uv run devdash-gui
```

Tkinterはpipの依存として導入するライブラリではありません。Ubuntuでは利用するPythonに対応したOSパッケージが必要です。
別配布のPythonで作ったvenvでは、`python3-tk`導入後もそのPythonがTkを持っていなければ利用できません。

## 確認したライセンス表記

2026-11-07に、実際にインストールしたwheelのMETADATAと同梱LICENSEを確認しました。

| 対象 | 確認した表記 |
| --- | --- |
| devdash | MIT OR Apache-2.0 |
| ttkbootstrap 2.2.7のMETADATA | `MIT AND (Apache-2.0 OR BSD-2-Clause)` |
| ttkbootstrapの本体LICENSE | MIT |
| 同梱Bootstrap IconsのLICENSE | MIT |
| Pillow 13.0.0のMETADATA・LICENSE | MIT-CMU |
| Tkinter | Python標準ライブラリ。Pythonのライセンスに従う |
| Tcl/Tk | BSD系のTcl/Tk license。OSの配布物に付属する表記も参照 |

`gui` extraの第三者パッケージはttkbootstrapとPillowです。PySide6/shiboken6を`pyproject.toml`と`uv.lock`から除き、同期後のvenvにも残っていないことを確認しました。
本体だけを見てGUI依存全体を「MITのみ」とは表記しません。Pillow wheel等が同梱するネイティブライブラリの通知も、単体アプリとして再配布する場合は配布物に合わせて保持します。
この変更では依存ライブラリを取り込んだインストーラーや単体実行ファイルは生成していません。

資料:
[ttkbootstrapの配布](https://pypi.org/project/ttkbootstrap/)、
[本体LICENSE](https://github.com/israel-dryer/ttkbootstrap/blob/master/LICENSE)、
[公式インストール手順](https://www.ttkbootstrap.org/en/latest/user-guide/getting-started/installation.html)、
[Tkのライセンス原文](https://github.com/tcltk/tk/blob/main/license.terms)、
[PillowのLICENSE](https://github.com/python-pillow/Pillow/blob/main/LICENSE)、
[Pythonのライセンス](https://docs.python.org/3/license.html)。

## 検証

GUIテストは実際のTkinterウィジェットとUnix Socketを使い、別スレッドのCore + Fake Backendへ接続します。
Start→Manual Gate承認→完了、部分Restart、Reinitialize→Cancel、ログ表示、個別StartによるGate迂回を確認します。
さらにuser/systemのScan切替、候補選択のリセット、Importによる設定・バックアップ保存も確認します。

```bash
DEVDASH_GUI_TEST=1 uv run pytest -q tests/python/test_gui.py
# CI等の画面なし環境（xvfbを別途導入）
DEVDASH_GUI_TEST=1 xvfb-run -a uv run pytest -q tests/python/test_gui.py
```

開発環境にはXvfbがなかったため、利用可能なDISPLAYと一時的なXephyr画面で確認しています。
全体検証には専用user unitと専用Docker fixtureも使用し、既存の実サービスは操作しません。

現在も大規模ツリー、Waylandネイティブ描画、ROS子要素を通したGUI E2E、単体アプリ配布は未検証です。
Tkinterはこの環境ではTcl/Tk 8.6とX11/Xwaylandを使用します。
