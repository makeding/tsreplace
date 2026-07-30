
# tsreplace <!-- omit in toc -->
by rigaya

tsの映像部分のみの置き換えを行い、サイズの圧縮を図るツールです。音声含め、他のパケットはそのままコピーします。

<img src="./data/tsreplace_concept.webp" width="800px">

## 目次 <!-- omit in toc -->
- [想定動作環境](#想定動作環境)
- [基本的な使用方法](#基本的な使用方法)
- [エンコードしながら置き換える](#エンコードしながら置き換える)
- [追加オプション](#追加オプション)
- [具体的な使用例](#具体的な使用例)
- [具体的な使用例 (インタレ保持)](#具体的な使用例-インタレ保持)
- [全オプション一覧](#全オプション一覧)
- [制限事項](#制限事項)
- [ソースコードについて](#ソースコードについて)
- [謝辞](#謝辞)
- [ソースの構成](#ソースの構成)

## 想定動作環境
### Windows
Windows 10/11 (x86/x64)  

### Linux
Ubuntu 20.04 - 24.04 (x64) ほか

### macOS
macOS 15 (Apple Silicon / arm64)

---

## 基本的な使用方法

tsreplaceを使用するには、コマンドラインから直接使用する方法と、[Amatsukaze(GUI)](https://github.com/rigaya/Amatsukaze)から使用する方法が存在します。ここでは、コマンドラインから直接使用する方法を記載します。

まず、オリジナルのtsをエンコードします。出力ファイルはmp4,mkv,ts等、timstampを保持できる形式にします(raw ES不可)。のちに作る映像置き換えtsファイルのシークを円滑にするため、GOP長はデフォルトより短めのほうがよいと思います。

`QSVEncC64.exe -i <入力tsファイル> [インタレ解除等他のオプション] --gop-len 90 -o <置き換え映像ファイル>`

次にtsreplaceを使って、エンコードした映像に置き換えたtsを作成します。

`tsreplace.exe -i <入力tsファイル> -r <置き換え映像ファイル> -o <出力tsファイル>`

## エンコードしながら置き換える
下記のように、2段階に分けずエンコードしながら置き換えを行うこともできます。

### tsreplaceでエンコーダを起動する方法

tsreplaceの```-e```オプションで指定のパスのエンコーダを起動し、置き換え映像を生成することができます。このとき、```-r```は指定しません。

```-e```後の引数は、_すべて_ エンコーダのオプションとして解釈される点に注意してください。また、エンコーダには、標準入力で受け取り、標準出力に出力するようオプションを記述する必要があります。

`tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e QSVEncC64.exe -i - --input-format mpegts [インタレ解除等他のオプション] --gop-len 90 --output-format mpegts -o -`

<img src="./data/tsreplace_internal_encoder.webp" width="640px">

### 置き換え映像ファイルを標準入力で受け取る方法

置き換え映像ファイルを下記のように標準入力で受け取ることもできます。**この場合、Powershellを使用するとPowershell側のパイプ渡しの問題でうまく動作しないため、コマンドプロンプトをご利用ください。**

`QSVEncC64.exe -i <入力tsファイル> [インタレ解除等他のオプション] --gop-len 90 --output-format mpegts -o - | tsreplace.exe -i <入力tsファイル> -r - -o <出力tsファイル>`

---

## 追加オプション

- 置き換えるサービスを指定する

  全サービスを録画しているtsファイルを対象にする場合、```--service``` で処理するサービスを指定することができます。

- データ放送を削除してさらに圧縮する

  データ放送のパケットも含まれるtsファイルを対象にする場合、データサイズに占める割合が比較的大きいことがあります。データ放送が不要な場合には```--remove-typed```オプションを追加すると、データ放送のパケットを削除することができます。

```--smart-remove-typed```を指定すると、エントリコンポーネントとPMTのType-D定義を常時保持しつつ、その他のデータ放送を14.5分ごとに1分間保持し、それ以外の区間のみ削除します。予定時間を渡した場合は末尾1分間も保持します。置き換え映像を指定せず、```-i```と```-o```だけでType-Dのtrimを行うこともできます。

---

## 具体的な使用例

下記では、インタレ解除してエンコードする例を示します。インタレ保持する場合は、[具体的な使用例 (インタレ保持)](#具体的な使用例-インタレ保持)を参照してください。

### ハードウェアエンコード

- Intel QSV

  QSVを使用する場合、[QSVEncC](https://github.com/rigaya/QSVEnc)を使用します。

  - H.264 エンコード  
    `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e QSVEncC64.exe -i - --input-format mpegts --tff --vpp-deinterlace normal -c h264 --icq 23 --gop-len 90 --output-format mpegts -o -`

  - HEVC エンコード  
    `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e QSVEncC64.exe -i - --input-format mpegts --tff --vpp-deinterlace normal -c hevc --icq 23 --gop-len 90 --output-format mpegts -o -`

- NVENC

  NVENCを使用する場合、[NVEncC](https://github.com/rigaya/NVEnc)を使用します。

  - H.264 エンコード  
    `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e NVEncC64.exe -i - --input-format mpegts --tff --vpp-deinterlace normal -c h264 --qvbr 23 --gop-len 90 --output-format mpegts -o -`

  - HEVC エンコード  
    `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e NVEncC64.exe -i - --input-format mpegts --tff --vpp-deinterlace normal -c hevc --qvbr 23 --gop-len 90 --output-format mpegts -o -`

### ソフトウェアエンコード

ソフトウェアエンコードを使用する場合、ffmpegを用います。

- x264

  `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e ffmpeg.exe -y -f mpegts -i - -copyts -start_at_zero -vf yadif -an -c:v libx264 -preset slow -crf 23 -g 90 -f mpegts -`

- x265

  `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e ffmpeg.exe -y -f mpegts -i - -copyts -start_at_zero -vf yadif -an -c:v libx265 -preset medium -crf 23 -g 90 -f mpegts -`

## 具体的な使用例 (インタレ保持)

インタレ保持の場合はH.264を使用します。HEVCはサポートしません。

### ハードウェアエンコード

ハードウェアエンコードの場合、インタレ保持に対応したハードウェアが必要です。

- Intel QSV

  インタレ保持にはPGモードの使用可能なGPUが必要です。(Arc GPUでは使用できません)

  `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e QSVEncC64.exe -i - --input-format mpegts --tff -c h264 --icq 23 --gop-len 90 --output-format mpegts -o -`
  
- NVENC

  インタレ保持にはGTX1xxx以前のGPUが必要です。

  `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e NVEncC64.exe -i - --input-format mpegts --tff -c h264 --qvbr 23 --gop-len 90 --output-format mpegts -o -`

### ソフトウェアエンコード

- x264  

  `tsreplace.exe -i <入力tsファイル> -o <出力tsファイル> -e ffmpeg.exe -y -f mpegts -i - -copyts -start_at_zero -an -c:v libx264 -flags +ildct+ilme -preset slow -crf 23 -g 90 -f mpegts -`

---

## 全オプション一覧

### -o, --output &lt;string&gt;
出力tsファイルのファイルパス。"-"で標準出力になります。

### -i, --input &lt;string&gt;
入力tsファイルのファイルパス。"-"で標準入力になります。

### -r, --replace &lt;string&gt;
置き換える映像の入っているファイルのパス。"-"で標準入力になります。

timestampを保持できるコンテナ入りの映像を想定しており、raw ES等は考慮しません。また、```-e```, ```--encoder```との併用はできません。

### -e, --encoder &lt;string&gt; [&lt;string&gt;]...
```-r```を指定する代わりに、指定のエンコーダを起動してエンコーダを行います。```-r```との併用はできません。

```-e```, ```--encoder```後は、エンコーダのパスとその引数として扱います。具体的な指定方法は、使用例を確認してください。

### -s, --service &lt;int&gt; or &lt;string&gt;
処理対象のサービスを指定します。

- &lt;int&gt; の場合  
  処理対象のサービスIDを直接指定します。

- &lt;string&gt; の場合  
  ```1st, 2nd, 3rd, ....``` で、処理対象のサービスをサービスの並び順に従って指定します。

### --preserve-other-services
処理対象でないサービスのパケットも保存します。(デフォルト：オフ)

### --copy-filets
入力ファイルのタイムスタンプを出力ファイルにコピーします。(デフォルト：オフ)

### --start-point &lt;string&gt;
置き換え時の時刻の起点を指定します。

- **パラメータ**
  - keyframe (デフォルト)  
    最初のキーフレームの時刻を起点とします。tsファイルを[QSVEncC](https://github.com/rigaya/QSVEnc)/[NVEncC](https://github.com/rigaya/NVEnc)/[VCEEncC](https://github.com/rigaya/VCEEnc)/[rkmppenc](https://github.com/rigaya/rkmppenc)でエンコードした場合やffmpegでエンコードした場合に映像のみ処理した場合に使用します。

  - firstframe  
    最初のフレームの時刻を起点とします。tsファイルをlwinput.auiで読み込みエンコードした場合に使用します。

  - firstpacket  
    映像・音声の最初のパケットの時刻を起点とします。

### --replace-delay &lt;int&gt;
置き換える映像の、オリジナルとの遅れを90kHz単位で指定します。

遅らせた時刻の分だけ、元のtsのパケット(映像・音声・その他含む)を出力せずにカットし、置き換え対象とのずれを修正します。

`--start-point` は置き換え映像の時刻合わせの基準であり、`--replace-delay` とは独立して動作します。

### --end-at-replace-eof [&lt;int&gt;]
置き換える映像ファイルのEOFに到達した時点から、指定した余裕時間(ms)後にTS出力を終了します。

- 引数省略時  
  100ms後に出力を終了します。

- &lt;int&gt; を指定した場合  
  指定した値をms単位の余裕時間として使用します。例: `--end-at-replace-eof 200` でEOF+200ms時点で出力終了。

### --replace-format &lt;string&gt;
置き換える映像の入っているファイルのフォーマットを指定します。

### --add-aud
映像パケットごとにAUDを自動挿入します。(デフォルト：オン)

### --no-add-aud
映像パケットごとのAUDの自動挿入を無効にします。

### --add-headers
映像のキーフレームごとにヘッダを自動挿入します。(デフォルト：オン)

### --no-add-headers
映像のキーフレームごとのヘッダの自動挿入を無効にします。

### --remove-typed
データ放送のパケットを削除し、さらなる圧縮を図ります。(デフォルト：オフ)

具体的には、データ放送で使用されているISO/IEC 13818-6 type DのPIDストリームのパケットを削除します。

映像を置き換えず、Type-Dのみを削除する場合は次のように実行します。この場合、入力TS内の全サービスを保持します。

`tsreplace -i <入力tsファイル> -o <出力tsファイル> --remove-typed`

### --smart-remove-typed

入力TSの先頭から14.5分ごとに1分間、ISO/IEC 13818-6 type Dを保持し、それ以外の区間から削除します。ただしデータ放送の起動に必要なエントリコンポーネントは常時保持し、PMTのType-D定義も削除しません。trim後にその他のType-D出力を再開する際は、次のpayload unit開始まで待って中途半端なsectionを出力しないようにします。この周期を入力のEOFまで繰り返すため、事前に入力時間を取得する必要はありません。

映像を置き換えずにtrimのみを行う場合は次のように実行します。

`tsreplace -i <入力tsファイル> -o <出力tsファイル> --smart-remove-typed`

標準入力・標準出力にも対応します。ログは標準エラーへ出力されます。

`tsreplace -i - -o - --smart-remove-typed`

呼び出し側が予定終了時間を把握している場合は、入力開始から終了までの秒数を指定すると最後の1分間も追加で保持します。

`tsreplace -i - -o - --smart-remove-typed-duration 1800`

ディレクトリ内の録画済みTSを一括処理する場合は、同梱のスクリプトを使用できます。
各ファイルを同じディレクトリの `<元ファイル名>.processing` にtrimし、TSDuckの
`tsanalyze`で188バイト境界とサービス情報を確認した後にのみ元ファイルを原子的に
置き換えます。処理中に元ファイルが更新された場合や検証に失敗した場合、元ファイルは
変更されません。処理を開始する前に`tsanalyze --version`が成功することを確認し、
各ファイルについて元ファイルの2倍以上の空き容量が同じファイルシステムにある場合
のみ処理します。また、EIT present/following (PID 0x0012) の番組タイトルに
`紅白歌合戦` または `開票速報` が含まれるファイルは、変化するデータ放送を
保存するため処理しません。番組説明や別イベントに含まれる文字列は判定に使用しません。
追加の保護語は
`--protect-keyword <文字列>`、この保護を無効にする場合は
`--no-protect-keywords`を指定します。

データ放送の削減効果がほぼないことを確認したBS11（network ID 4 / service ID 211）と
110度CS（network ID 6、7）は、デフォルトでtrim自体を開始せずにスキップします。
110度CSには契約案内などのデータ放送が含まれますが、サンプルでは全Type-Dを
削除しても2.2 MiB程度しか減らず、バッチ処理の10 MiB閾値に届きませんでした。
この内蔵リストを無効にする場合は `--no-skip-channels` を指定します。

`./scripts/trim_directory.py .`

サブディレクトリも対象にする場合は `--recursive`、元ファイルを `.original` として
残す場合は `--backup-suffix .original`、元ファイルを置き換えずに
`<元ファイル名>-trimed.ts` または `.m2ts` として出力する場合は `--no-replace`、
実行内容だけ確認する場合は `--dry-run` を指定します。

候補ファイルの削減量が10 MiB未満の場合は候補を削除し、元ファイルを置き換えません。
閾値は `--minimum-savings-mib` で変更でき、0を指定すると無効になります。

通常実行では、対象ディレクトリの `tsreplace-trim-report.tsv` に各ファイルの結果を
逐次追記します。ファイル名、EITから取得した番組名・番組開始／終了時刻・番組時間、
実処理の開始／終了時刻・所要時間、処理結果、trim前後のサイズ・削減率、サービスID、
エラー内容を記録します。EITを取得できないファイルは番組情報を空欄にしたまま処理を
継続します。出力先は `--report <ファイル>` で変更でき、不要な場合は
`--no-report` を指定します。TSVはファイルごとの処理直後に同期して書き込むため、
バッチ処理が途中で停止しても、それまでの結果は残ります。
次回起動時には既存のTSVを読み込み、成功済み、保護対象、内蔵チャンネルリスト対象、
削減量が閾値未満だったファイルを、現在のファイルサイズも一致する場合にスキップします。
失敗したファイルは完了扱いにせず、次回もう一度処理します。

### --log &lt;string&gt;
ログを指定のファイルに出力します。

### --log-level &lt;string&gt;
ログ出力のレベルを下記から選択します。

```
- debug, info(default), warn, error
```

---

## 制限事項

下記については、対応予定はありません。

- カット編集等の行われたtsおよび映像ファイルは、置き換え時の同期が困難なため非対応です。
- 音声・字幕等、映像以外に関わる処理
- 入力tsファイルの制限
  - 188byte tsのみ対応しています。
  - 解像度変更のあるtsについては動作は検証しません。
- 置き換え映像ファイルの制限
  - H.264/HEVCの置き換えのみ対応します。
  - インタレ保持はH.264のみ対応します。
  - 置き換えファイルはtimestampを保持できるコンテナ入りの映像を想定しています。
    ESでの動作は検証しません。

## ソースコードについて
- MITライセンスです。
- 本ソフトウェアでは、
  [ffmpeg](https://ffmpeg.org/)
  を使用しています。

## 謝辞
本ソフトウェア作成に当たり、
[tsreadex](https://github.com/xtne6f/tsreadex)を大変参考にさせていただきました。  
どうもありがとうございました。


## ソースの構成
Windows ... VCビルド  

文字コード: UTF-8-BOM  
改行: CRLF  
インデント: 空白x4  
