#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kana_drill.py  ―  幼児向け「毎日1ページ」かなドリル生成 + 間隔反復（SRS）復習

毎日1回実行すると、その日の 問題1枚（子どもに渡す用）と 答え1枚（まるつけ用）を PDF で出す。
カタカナ主体・ひらがなは復習枠、という配分で、まちがえた字は間隔反復で出し直す。

セクション（出題数は環境変数で変えられる。0 にすると紙面から消える）:
- K   カタカナを なぞろう      うすい見本をなぞり、対応するひらがな（ア＝あ）を併記して音を結びつける
- KC  にた字、どっち?           シとツ・ソとン・クとワ など、カタカナで最もまちがえやすい組を選ばせる
- KE  カタカナ しりとり         1つ目のことばを見て、2つ目のことばを絵をヒントに全部書く
- A   ひらがな ふくしゅう       SRS が「今日出す」と判定した未卒業の字だけを なぞる（全部卒業すると消える）
- E   ひらがな しりとり         既定では 0（ひらがなをメインにしたいときに戻す）

出題はプールから日替わりで選び、SQLite で「最近出した順」を持ってしばらく重複しないよう回す。
review.txt に1行1字で書いた字（または srs-mark で足した字）は優先的に出題し、
review_state.json で字ごとの段階と次回出題日を管理する。

依存:  pip install playwright && playwright install chromium
       日本語フォント（Noto Sans CJK JP など）が入っていること。
実行:  python3 kana_drill.py                       # 今日のぶん
       python3 kana_drill.py --date 2026-06-22     # 日付指定
       python3 kana_drill.py --html-only           # PDF を作らず HTML だけ出す（playwright 不要）
       python3 kana_drill.py srs-mark miss の は    # まちがえた字を翌日リセット
       python3 kana_drill.py srs-mark correct さ し # 正解した字の間隔をのばす
       python3 kana_drill.py srs-status            # いまの due 状況
"""

import os, sys, sqlite3, random, datetime, tempfile, pathlib
# playwright はPDF生成時のみ必要。SRSサブコマンド(srs-mark/srs-status)では不要なので
# 遅延importにして、playwright未導入の環境でも採点反映だけは動くようにする。

# ── 設定 ───────────────────────────────────────────────
# 状態と出力の置き場。既定はカレントディレクトリの data/。
BASE   = pathlib.Path(os.environ.get("KANA_DRILL_DIR", "data")).expanduser()
OUTDIR = BASE / "out"
DB     = BASE / "state.db"
REVIEW = BASE / "review.txt"          # 任意: 復習したい字を1行ずつ
N_A, N_E, N_K, N_KC, N_KE = 2, 0, 3, 2, 4   # 各セクションの出題数（計11問で A4 1枚に収まる量）
# 配分の考え方:
#   - 1日の総量は増やさない。難しくしたいときは、セクション間で問数を付け替える
#   - K/KC/KE（カタカナ）がメイン。A（ひらがな）は SRS が due と判定した未卒業字だけが母集合なので、
#     ひらがなを全部卒業すると自動的に 0 問になり紙面から消える（急に打ち切らず自然に消す）
#   - E（ひらがな しりとり）はプールとロジックを残してある。ひらがな主体に戻すなら N_E を上げる
# 環境変数で各セクションの出題数を上書きできる（構成違いパターンの生成用）。
def _env_int(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        sys.exit(f"[ERROR] {name} は整数で指定してください: {v!r}")
N_A  = _env_int("KANA_DRILL_N_A",  N_A)
N_E  = _env_int("KANA_DRILL_N_E",  N_E)
N_K  = _env_int("KANA_DRILL_N_K",  N_K)
N_KC = _env_int("KANA_DRILL_N_KC", N_KC)
N_KE = _env_int("KANA_DRILL_N_KE", N_KE)
# 出力ファイル名の末尾サフィックス(同日に複数パターンを出し分けるため)。例: "_p1"
OUT_SUFFIX = os.environ.get("KANA_DRILL_OUT_SUFFIX", "")
# 子どもの名前。紙面の見出しに入る。空なら「カタカナ ドリル」とだけ出す
CHILD  = os.environ.get("KANA_DRILL_NAME", "")
# 想定年齢。紙面の肩書きに使う（例: 5 → 「5さい」）。空なら出さない
AGE    = os.environ.get("KANA_DRILL_AGE", "")
# 範囲: ひらがな 清音46 + 濁音・半濁音23 + 促音・拗音・四つ仮名6 = 75字、カタカナ 45字 + 長音ー。
# ヲ は現代語でカタカナ表記の用例がほぼ無いため除外している。
# 範囲を狭めたいときは review.txt に対象の字だけを書く（A は due の字だけを出すため、
# review.txt に無い字は出ない）。

# ── 問題プール ─────────────────────────────────────────
# A: なぞり書き。char=なぞる文字。 words=その字で始まる例語のリスト(声に出す用)。
# 出題のたびに rng で1つランダムに選ぶ(同じ字でも毎回同じ単語にならないようにするため)。
# を・ん は「その字で始まる語」が存在しないので特別扱い: を=をを含む短いフレーズ、
# ん=その字で終わる語。字ごとに複数語を持たせ、同じ字が出るたび毎回同じ単語にならないようにしている。
POOL_A = [
    dict(id="a_a",  char="あ", words=["あり", "あめ", "あひる"]),
    dict(id="a_i",  char="い", words=["いぬ", "いえ", "いちご"]),
    dict(id="a_u",  char="う", words=["うま", "うさぎ", "うちわ"]),
    dict(id="a_e",  char="え", words=["えんぴつ", "えほん", "えき"]),
    dict(id="a_o",  char="お", words=["おに", "おかし", "おふろ"]),
    dict(id="a_ka", char="か", words=["かさ", "かめ", "かえる"]),
    dict(id="a_ki", char="き", words=["きつね", "きりん", "きのこ"]),
    dict(id="a_ku", char="く", words=["くるま", "くつ", "くま"]),
    dict(id="a_ke", char="け", words=["けむし", "けしごむ"]),
    dict(id="a_ko", char="こ", words=["こま", "こおり", "こども"]),
    dict(id="a_sa", char="さ", words=["さかな", "さる", "さくら"]),
    dict(id="a_shi",char="し", words=["しか", "しま", "しっぽ"]),
    dict(id="a_su", char="す", words=["すいか", "すし", "すな"]),
    dict(id="a_se", char="せ", words=["せみ", "せんぷうき", "せっけん"]),
    dict(id="a_so", char="そ", words=["そら", "そうじき", "そり"]),
    dict(id="a_ta", char="た", words=["たこ", "たいこ", "たまご"]),
    dict(id="a_chi",char="ち", words=["ちず", "ちきゅう", "ちょう"]),
    dict(id="a_tsu",char="つ", words=["つき", "つくえ", "つの"]),
    dict(id="a_te", char="て", words=["てがみ", "てぶくろ", "てんき"]),
    dict(id="a_to", char="と", words=["とけい", "とんぼ", "とり"]),
    dict(id="a_na", char="な", words=["なす", "なわとび", "なつ"]),
    dict(id="a_ni", char="に", words=["にじ", "にわとり", "にんじん"]),
    dict(id="a_nu", char="ぬ", words=["ぬりえ", "ぬいぐるみ", "ぬの"]),
    dict(id="a_ne", char="ね", words=["ねこ", "ねずみ", "ねぎ"]),
    dict(id="a_no", char="の", words=["のり", "のはら", "のみもの"]),
    dict(id="a_ha", char="は", words=["はな", "はさみ", "はち"]),
    dict(id="a_hi", char="ひ", words=["ひこうき", "ひまわり", "ひも"]),
    dict(id="a_fu", char="ふ", words=["ふね", "ふうせん", "ふでばこ"]),
    dict(id="a_he", char="へ", words=["へび", "へや"]),
    dict(id="a_ho", char="ほ", words=["ほし", "ほん", "ほうき"]),
    dict(id="a_ma", char="ま", words=["まめ", "まくら", "まど"]),
    dict(id="a_mi", char="み", words=["みかん", "みず", "みみ"]),
    dict(id="a_mu", char="む", words=["むし", "むらさき", "むぎちゃ"]),
    dict(id="a_me", char="め", words=["めがね", "めろん", "めだか"]),
    dict(id="a_mo", char="も", words=["もも", "もり", "もぐら"]),
    dict(id="a_ya", char="や", words=["やま", "やさい", "やかん"]),
    dict(id="a_yu", char="ゆ", words=["ゆき", "ゆびわ", "ゆめ"]),
    dict(id="a_yo", char="よ", words=["よる", "ようふく"]),
    dict(id="a_ra", char="ら", words=["らっぱ", "らいおん", "らくだ"]),
    dict(id="a_ri", char="り", words=["りんご", "りす", "りぼん"]),
    dict(id="a_ru", char="る", words=["るすばん"]),
    dict(id="a_re", char="れ", words=["れいぞうこ", "れもん"]),
    dict(id="a_ro", char="ろ", words=["ろうそく", "ろば"]),
    dict(id="a_wa", char="わ", words=["わに", "わたあめ", "わごむ"]),
    dict(id="a_wo", char="を", words=["ほんを よむ", "えを かく", "みずを のむ"]),
    dict(id="a_n",  char="ん", words=["ぱん", "かばん", "れんこん"]),
    # ── 濁音・半濁音 ──────────────────────
    # 清音が概ね定着した段階で、次の段として が行〜ぱ行 23字を足す想定。
    # 字形は清音+点/丸なので運筆の負荷は低く、「点をつけると音が濁る」という音の対応を
    # おぼえるのが主目的。ぱ行は外来語がひらがな表記になると不自然な語が多いため、
    # ぱたぱた・ぴかぴか のような擬音語を必ず1語混ぜてある。
    # ぢ・づ は「その字で始まる語」が日本語にほぼ存在せず(四つ仮名)、幼児には早いので
    # 意図的に外してある。必要になったら を・ん と同じ特別扱いで足すこと。
    dict(id="a_ga", char="が", words=["がっこう", "がようし"]),
    dict(id="a_gi", char="ぎ", words=["ぎゅうにゅう", "ぎんいろ"]),
    dict(id="a_gu", char="ぐ", words=["ぐみ", "ぐるぐる"]),
    dict(id="a_ge", char="げ", words=["げた", "げんき"]),
    dict(id="a_go", char="ご", words=["ごりら", "ごはん", "ごま"]),
    dict(id="a_za", char="ざ", words=["ざりがに", "ざぶとん"]),
    dict(id="a_ji", char="じ", words=["じてんしゃ", "じどうしゃ"]),
    dict(id="a_zu", char="ず", words=["ずかん", "ずぼん"]),
    dict(id="a_ze", char="ぜ", words=["ぜんぶ", "ぜんまい"]),
    dict(id="a_zo", char="ぞ", words=["ぞう", "ぞうきん"]),
    dict(id="a_da", char="だ", words=["だいこん", "だんご"]),
    dict(id="a_de", char="で", words=["でんしゃ", "でんわ"]),
    dict(id="a_do", char="ど", words=["どんぐり", "どうぶつ"]),
    dict(id="a_ba", char="ば", words=["ばなな", "ばった"]),
    dict(id="a_bi", char="び", words=["びわ", "びょういん"]),
    dict(id="a_bu", char="ぶ", words=["ぶどう", "ぶた"]),
    dict(id="a_be", char="べ", words=["べんとう", "べんきょう"]),
    dict(id="a_bo", char="ぼ", words=["ぼうし", "ぼたん"]),
    dict(id="a_pa", char="ぱ", words=["ぱんだ", "ぱたぱた"]),
    dict(id="a_pi", char="ぴ", words=["ぴあの", "ぴかぴか"]),
    dict(id="a_pu", char="ぷ", words=["ぷりん", "ぷかぷか"]),
    dict(id="a_pe", char="ぺ", words=["ぺんぎん", "ぺこぺこ"]),
    dict(id="a_po", char="ぽ", words=["ぽすと", "ぽかぽか"]),
    # ── 促音・拗音・四つ仮名 ────────────────
    # いずれも「その字で始まる語」が存在しないので、を と同じ "含む" 判定にしてある
    # (_check_a の CONTAINS_CHARS)。note は紙面の右側に小さく出す注意書きで、
    # 小書き文字(っゃゅょ)を マスの左下に ちいさく 書くこと(横書きの決まり)を毎回
    # 思い出させるためのもの。見本自体もマスの左下に寄せて描画する(CSS .trace-box.sk)。
    # ぢ・づ は四つ仮名。じ・ず と音が同じで書き分けが要るため、語をよく声に出させること。
    dict(id="a_sokuon", char="っ", words=["らっぱ", "きって", "がっこう", "ばった"],
         note="ちいさく ひだりしたに"),
    dict(id="a_ya_s",  char="ゃ", words=["おもちゃ", "ちゃわん", "じてんしゃ"],
         note="ちいさく ひだりしたに"),
    dict(id="a_yu_s",  char="ゅ", words=["きゅうり", "しゅくだい", "ぎゅうにゅう"],
         note="ちいさく ひだりしたに"),
    dict(id="a_yo_s",  char="ょ", words=["ちょうちょ", "きんぎょ", "しょくぱん"],
         note="ちいさく ひだりしたに"),
    dict(id="a_dji",   char="ぢ", words=["はなぢ", "ちぢむ"], note="ち に てんてん"),
    dict(id="a_dzu",   char="づ", words=["みかづき", "こづつみ", "つづく"], note="つ に てんてん"),
]

# K: カタカナの なぞり書き。
# char=なぞるカタカナ。yomi=対応するひらがな(見比べて対応づけさせる)。words=その字で始まる語。
# ひらがなが概ね定着し、絵本・おもちゃの表記でカタカナに触れる機会が増えた頃に、ひらがなと並行で始める想定。
# 語は「ひらがなでは書かない語(外来語・擬音・カタカナ表記が自然な動植物)」を優先している。
# ヲ は現代語で用例がほぼ無いため不採用。ン は語頭に立たないので を・ん と同じ特別扱い
# (末尾一致)、ー(長音)は "含む" 判定にしている。小書きのャュョッは ひらがなの ゃゅょっ を
# 卒業してから足すこと(カタカナ側には入れていない)。
POOL_K = [
    dict(id="k_a",  char="ア", yomi="あ", words=["アイス", "アイロン"]),
    dict(id="k_i",  char="イ", yomi="い", words=["イルカ", "イス"]),
    dict(id="k_u",  char="ウ", yomi="う", words=["ウサギ", "ウインナー"]),
    dict(id="k_e",  char="エ", yomi="え", words=["エプロン", "エアコン"]),
    dict(id="k_o",  char="オ", yomi="お", words=["オレンジ", "オムレツ"]),
    dict(id="k_ka", char="カ", yomi="か", words=["カメラ", "カレー"]),
    dict(id="k_ki", char="キ", yomi="き", words=["キリン", "キウイ"]),
    dict(id="k_ku", char="ク", yomi="く", words=["クレヨン", "クッキー"]),
    dict(id="k_ke", char="ケ", yomi="け", words=["ケーキ", "ケチャップ"]),
    dict(id="k_ko", char="コ", yomi="こ", words=["コアラ", "コップ"]),
    dict(id="k_sa", char="サ", yomi="さ", words=["サラダ", "サンダル"]),
    dict(id="k_shi",char="シ", yomi="し", words=["シマウマ", "シール"]),
    dict(id="k_su", char="ス", yomi="す", words=["スプーン", "スカート"]),
    dict(id="k_se", char="セ", yomi="せ", words=["セーター", "セロテープ"]),
    dict(id="k_so", char="ソ", yomi="そ", words=["ソファ", "ソーセージ"]),
    dict(id="k_ta", char="タ", yomi="た", words=["タオル", "タクシー"]),
    dict(id="k_chi",char="チ", yomi="ち", words=["チーズ", "チキン"]),
    dict(id="k_tsu",char="ツ", yomi="つ", words=["ツナ", "ツリー"]),
    dict(id="k_te", char="テ", yomi="て", words=["テレビ", "テーブル"]),
    dict(id="k_to", char="ト", yomi="と", words=["トマト", "トラック"]),
    dict(id="k_na", char="ナ", yomi="な", words=["ナイフ", "ナプキン"]),
    dict(id="k_ni", char="ニ", yomi="に", words=["ニンジン", "ニット"]),
    dict(id="k_nu", char="ヌ", yomi="ぬ", words=["ヌードル"]),
    dict(id="k_ne", char="ネ", yomi="ね", words=["ネクタイ", "ネット"]),
    dict(id="k_no", char="ノ", yomi="の", words=["ノート", "ノック"]),
    dict(id="k_ha", char="ハ", yomi="は", words=["ハンカチ", "ハート"]),
    dict(id="k_hi", char="ヒ", yomi="ひ", words=["ヒーター", "ヒヨコ"]),
    dict(id="k_fu", char="フ", yomi="ふ", words=["フォーク", "フルーツ"]),
    dict(id="k_he", char="ヘ", yomi="へ", words=["ヘルメット", "ヘリコプター"]),
    dict(id="k_ho", char="ホ", yomi="ほ", words=["ホース", "ホテル"]),
    dict(id="k_ma", char="マ", yomi="ま", words=["マスク", "マイク"]),
    dict(id="k_mi", char="ミ", yomi="み", words=["ミルク", "ミシン"]),
    dict(id="k_mu", char="ム", yomi="む", words=["ムース", "ムービー"]),
    dict(id="k_me", char="メ", yomi="め", words=["メロン", "メダル"]),
    dict(id="k_mo", char="モ", yomi="も", words=["モップ", "モデル"]),
    dict(id="k_ya", char="ヤ", yomi="や", words=["ヤカン", "ヤシ"]),
    dict(id="k_yu", char="ユ", yomi="ゆ", words=["ユニフォーム", "ユリ"]),
    dict(id="k_yo", char="ヨ", yomi="よ", words=["ヨット", "ヨーグルト"]),
    dict(id="k_ra", char="ラ", yomi="ら", words=["ライオン", "ラーメン"]),
    dict(id="k_ri", char="リ", yomi="り", words=["リンゴ", "リボン"]),
    dict(id="k_ru", char="ル", yomi="る", words=["ルーレット", "ルビー"]),
    dict(id="k_re", char="レ", yomi="れ", words=["レモン", "レストラン"]),
    dict(id="k_ro", char="ロ", yomi="ろ", words=["ロボット", "ロケット"]),
    dict(id="k_wa", char="ワ", yomi="わ", words=["ワニ", "ワカメ"]),
    dict(id="k_n",  char="ン", yomi="ん", words=["パン", "ライオン", "ミカン"]),
    dict(id="k_cho", char="ー", yomi="のばす", words=["ラーメン", "ケーキ", "スーパー"],
         note="よこに いっぽん のばす"),
]

# KC: にた字、どっち?(カタカナ)。
# カタカナは ひらがなより字形の差が小さく、シとツ・ソとンのように「点の向き」「はらいの向き」
# だけがちがう組が多い。ここを外すと語が読めなくなるので、カタカナ主体に切り替えるのと同時に
# 独立セクションとして常設した。ひらがなの「にた字どっち」に慣れていれば、カタカナでも入り口として機能する。
# 形式: 絵 + 1字だけ空マスにした語 + 候補2字。正しい方を空マスに書く。
#   word  : 出題する語(カタカナのみ)
#   idx   : 空マスにする位置。正解の字は word[idx] から自動で決まる
#   other : まちがいの候補(にている字)
# 候補の左右の並びは日替わりの rng でシャッフルするので、位置で覚えることはできない。
POOL_KC = [
    dict(id="c_shi_shimauma", emoji="🦓", word="シマウマ", idx=0, other="ツ"),
    dict(id="c_tsu_tsuri",    emoji="🎄", word="ツリー",   idx=0, other="シ"),
    dict(id="c_so_sori",      emoji="🛷", word="ソリ",     idx=0, other="ン"),
    dict(id="c_n_raion",      emoji="🦁", word="ライオン", idx=3, other="ソ"),
    dict(id="c_ku_miruku",    emoji="🥛", word="ミルク",   idx=2, other="ワ"),
    dict(id="c_wa_wani",      emoji="🐊", word="ワニ",     idx=0, other="ク"),
    dict(id="c_a_aisu",       emoji="🍨", word="アイス",   idx=0, other="マ"),
    dict(id="c_ma_tomato",    emoji="🍅", word="トマト",   idx=1, other="ア"),
    dict(id="c_su_basu",      emoji="🚌", word="バス",     idx=1, other="ヌ"),
    dict(id="c_nu_nudoru",    emoji="🍜", word="ヌードル", idx=0, other="ス"),
    dict(id="c_no_noto",      emoji="📓", word="ノート",   idx=0, other="ソ"),
    dict(id="c_chi_chizu",    emoji="🧀", word="チーズ",   idx=0, other="テ"),
    dict(id="c_te_terebi",    emoji="📺", word="テレビ",   idx=0, other="チ"),
    dict(id="c_ru_kaeru",     emoji="🐸", word="カエル",   idx=2, other="レ"),
    dict(id="c_re_remon",     emoji="🍋", word="レモン",   idx=0, other="ル"),
    dict(id="c_fu_furutsu",   emoji="🍇", word="フルーツ", idx=0, other="ワ"),
    dict(id="c_ko_koara",     emoji="🐨", word="コアラ",   idx=0, other="ユ"),
    dict(id="c_yu_yunikon",   emoji="🦄", word="ユニコーン", idx=0, other="コ"),
    dict(id="c_ne_nekutai",   emoji="👔", word="ネクタイ", idx=0, other="ホ"),
    dict(id="c_ho_hoteru",    emoji="🏨", word="ホテル",   idx=0, other="ネ"),
    dict(id="c_sa_sarada",    emoji="🥗", word="サラダ",   idx=0, other="セ"),
    dict(id="c_se_randoseru", emoji="🎒", word="ランドセル", idx=3, other="サ"),
    dict(id="c_mi_mishin",    emoji="🧵", word="ミシン",   idx=0, other="ニ"),
    dict(id="c_ni_ninjin",    emoji="🥕", word="ニンジン", idx=0, other="ミ"),
    dict(id="c_ta_tako",      emoji="🐙", word="タコ",     idx=0, other="ク"),
    dict(id="c_me_meron",     emoji="🍈", word="メロン",   idx=0, other="ヌ"),
    dict(id="c_ro_ropu",      emoji="🪢", word="ロープ",   idx=0, other="コ"),
    dict(id="c_i_iruka",      emoji="🐬", word="イルカ",   idx=0, other="リ"),
    dict(id="c_ri_ribon",     emoji="🎀", word="リボン",   idx=0, other="イ"),
    dict(id="c_ka_kamera",    emoji="📷", word="カメラ",   idx=0, other="タ"),
]

# KE: カタカナ しりとり。E(ひらがなしりとり)のカタカナ版。
# 形式は旧E を踏襲する: word1 は 絵+ことば全部を見せ、word2 は 絵だけをヒントに全部書かせる。
# **語の選定ルール(重要。_check_ke が自動検証する)**
#   - word1 は ン・長音ー・小書き(ャュョッァィゥェォ) で終わる語を使わない。
#     外来語は ラーメン・スーパー のように ン と ー で終わる語が非常に多く、そのままだと
#     しりとりが成立しないか「のばす音の前の母音を取る」という5歳には難しい処理が必要になる。
#   - word2 は制約なし。ライオン・メロン のような ン終わりも答えに使える(そこで終わりでよい)。
#   - word2 は書くマスの横幅の都合で2〜4文字。小書きを含む語は word2 に使わない
#     (カタカナの小書きは範囲外のため。ひらがなの っゃゅょ が卒業したら解禁する想定)。
#   - 濁音・半濁音は語の中には出す。パンダ・バナナ・ダンス は読めれば書ける想定。
#   - 長音ー は K で導入済みなので ケーキ・カレー を答えに使ってよい。
POOL_KE = [
    dict(id="ke_tomato_tora",    emoji1="🍅", word1="トマト",   emoji2="🐯", word2="トラ",     ans="ト"),
    dict(id="ke_miruku_kureyon", emoji1="🥛", word1="ミルク",   emoji2="🖍️", word2="クレヨン", ans="ク"),
    dict(id="ke_iruka_kamera",   emoji1="🐬", word1="イルカ",   emoji2="📷", word2="カメラ",   ans="カ"),
    dict(id="ke_kamera_ramen",   emoji1="📷", word1="カメラ",   emoji2="🍜", word2="ラーメン", ans="ラ"),
    dict(id="ke_koara_raion",    emoji1="🐨", word1="コアラ",   emoji2="🦁", word2="ライオン", ans="ラ"),
    dict(id="ke_banana_naifu",   emoji1="🍌", word1="バナナ",   emoji2="🔪", word2="ナイフ",   ans="ナ"),
    dict(id="ke_suika_kare",     emoji1="🍉", word1="スイカ",   emoji2="🍛", word2="カレー",   ans="カ"),
    dict(id="ke_terebi_biru",    emoji1="📺", word1="テレビ",   emoji2="🏢", word2="ビル",     ans="ビ"),
    dict(id="ke_panda_dansu",    emoji1="🐼", word1="パンダ",   emoji2="💃", word2="ダンス",   ans="ダ"),
    dict(id="ke_keki_kirin",     emoji1="🍰", word1="ケーキ",   emoji2="🦒", word2="キリン",   ans="キ"),
    dict(id="ke_sarada_daiya",   emoji1="🥗", word1="サラダ",   emoji2="💎", word2="ダイヤ",   ans="ダ"),
    dict(id="ke_gorira_rajio",   emoji1="🦍", word1="ゴリラ",   emoji2="📻", word2="ラジオ",   ans="ラ"),
    dict(id="ke_tako_kon",       emoji1="🐙", word1="タコ",     emoji2="🌽", word2="コーン",   ans="コ"),
    dict(id="ke_hasami_miruku",  emoji1="✂️", word1="ハサミ",   emoji2="🥛", word2="ミルク",   ans="ミ"),
    dict(id="ke_neko_koara",     emoji1="🐱", word1="ネコ",     emoji2="🐨", word2="コアラ",   ans="コ"),
    dict(id="ke_ringo_gorira",   emoji1="🍎", word1="リンゴ",   emoji2="🦍", word2="ゴリラ",   ans="ゴ"),
    dict(id="ke_tonakai_iruka",  emoji1="🦌", word1="トナカイ", emoji2="🐬", word2="イルカ",   ans="イ"),
    dict(id="ke_wani_ninjin",    emoji1="🐊", word1="ワニ",     emoji2="🥕", word2="ニンジン", ans="ニ"),
    dict(id="ke_yashi_shimauma", emoji1="🌴", word1="ヤシ",     emoji2="🦓", word2="シマウマ", ans="シ"),
    dict(id="ke_aisu_supun",     emoji1="🍨", word1="アイス",   emoji2="🥄", word2="スプーン", ans="ス"),
    dict(id="ke_kujira_rakuda",  emoji1="🐳", word1="クジラ",   emoji2="🐫", word2="ラクダ",   ans="ラ"),
    dict(id="ke_tora_rajio",     emoji1="🐯", word1="トラ",     emoji2="📻", word2="ラジオ",   ans="ラ"),
    dict(id="ke_hiyoko_koto",    emoji1="🐤", word1="ヒヨコ",   emoji2="🧥", word2="コート",   ans="コ"),
    dict(id="ke_zou_usagi",      emoji1="🐘", word1="ゾウ",     emoji2="🐰", word2="ウサギ",   ans="ウ"),
    dict(id="ke_usagi_gita",     emoji1="🐰", word1="ウサギ",   emoji2="🎸", word2="ギター",   ans="ギ"),
    dict(id="ke_naifu_furutsu",  emoji1="🔪", word1="ナイフ",   emoji2="🍇", word2="フルーツ", ans="フ"),
    dict(id="ke_masuku_kuma",    emoji1="😷", word1="マスク",   emoji2="🐻", word2="クマ",     ans="ク"),
    dict(id="ke_tento_tomato",   emoji1="⛺", word1="テント",   emoji2="🍅", word2="トマト",   ans="ト"),
    dict(id="ke_ropu_purin",     emoji1="🪢", word1="ロープ",   emoji2="🍮", word2="プリン",   ans="プ"),
    dict(id="ke_basu_suika",     emoji1="🚌", word1="バス",     emoji2="🍉", word2="スイカ",   ans="ス"),
    dict(id="ke_noto_tora",      emoji1="📓", word1="ノート",   emoji2="🐯", word2="トラ",     ans="ト"),
]

# E: しりとり。word1(絵+ことば全部を見せる)→word2(絵だけヒントに出し、ことば全部を空マスで
# 自分で書かせる)。ans = word1 の さいごの1文字 = word2 の さいしょの1文字(しりとりの
# つながりの検証・答え表示用に保持)。以前は word2 の さいしょの1字を word1 の さいごの字から
# そのまま写すだけだったが、「word2 を絵をヒントに ぜんぶ自分で書く」に強化して難易度を上げた。
## hint2: emoji2だけでは5歳児がことばを連想しにくい項目に付ける短い補足ヒント。
## 答えの文字そのものは書かず、「何をしている絵か」を言葉で補うだけにする。
POOL_E = [
    dict(id="e_ari_ringo",     emoji1="🐜",  word1="あり",   emoji2="🍎",  word2="りんご", ans="り"),
    dict(id="e_inu_nurie",     emoji1="🐶",  word1="いぬ",   emoji2="🎨",  word2="ぬりえ", ans="ぬ", hint2="いろをつける"),
    dict(id="e_uma_mame",      emoji1="🐴",  word1="うま",   emoji2="🫘",  word2="まめ",   ans="ま"),
    dict(id="e_kasa_sakana",   emoji1="☂️",  word1="かさ",   emoji2="🐟",  word2="さかな", ans="さ"),
    dict(id="e_sakana_nasu",   emoji1="🐟",  word1="さかな", emoji2="🍆",  word2="なす",   ans="な"),
    dict(id="e_nasu_suika",    emoji1="🍆",  word1="なす",   emoji2="🍉",  word2="すいか", ans="す"),
    dict(id="e_neko_koori",    emoji1="🐱",  word1="ねこ",   emoji2="🧊",  word2="こおり", ans="こ"),
    dict(id="e_mame_megane",   emoji1="🫘",  word1="まめ",   emoji2="👓",  word2="めがね", ans="め"),
    dict(id="e_megane_neko",   emoji1="👓",  word1="めがね", emoji2="🐱",  word2="ねこ",   ans="ね"),
    dict(id="e_tsuki_kitsune", emoji1="🌙",  word1="つき",   emoji2="🦊",  word2="きつね", ans="き"),
    dict(id="e_enpitsu_tsuki", emoji1="✏️",  word1="えんぴつ",emoji2="🌙", word2="つき",   ans="つ"),
    dict(id="e_sora_rappa",    emoji1="🌤️",  word1="そら",   emoji2="🎺",  word2="らっぱ", ans="ら"),
    dict(id="e_hoshi_shika",   emoji1="⭐",  word1="ほし",   emoji2="🦌",  word2="しか",   ans="し"),
    dict(id="e_tokei_inu",     emoji1="🕐",  word1="とけい", emoji2="🐶",  word2="いぬ",   ans="い"),
    dict(id="e_rousoku_kuruma",emoji1="🕯️",  word1="ろうそく",emoji2="🚗", word2="くるま", ans="く"),
    dict(id="e_umi_mimi",      emoji1="🌊",  word1="うみ",   emoji2="👂",  word2="みみ",   ans="み"),
    dict(id="e_kumo_mori",     emoji1="☁️",  word1="くも",   emoji2="🌲",  word2="もり",   ans="も", hint2="きがいっぱい"),
    dict(id="e_kani_niji",     emoji1="🦀",  word1="かに",   emoji2="🌈",  word2="にじ",   ans="に"),
    dict(id="e_kame_meron",    emoji1="🐢",  word1="かめ",   emoji2="🍈",  word2="めろん", ans="め"),
]

# ── SQLite 回転ロジック ─────────────────────────────────
def db_conn():
    BASE.mkdir(parents=True, exist_ok=True)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS usage(cat TEXT, item TEXT, last INT, PRIMARY KEY(cat,item))")
    return con

# ── 簡易間隔反復 (SRS) / 忘却曲線 ─────────────────────────
# review.txt = 復習対象の母集合(ひらがなのリスト)。# で始まる行はコメントで対象外。
# review_state.json = 字ごとの {stage, due} を管理し、忘却曲線で出題間隔をあける。
#   stage 0→1→2→3→4(卒業)。正解で stage+1・due を先へ、まちがいで stage0・翌日にリセット。
# due(次回出題日)を迎えた字だけ「今日のreview対象」として優先出題する。
import json as _json
SRS_STATE = BASE / "review_state.json"
SRS_INTERVALS = [1, 3, 7, 14]   # stage0→翌日, 1→3日, 2→1週, 3→2週。stage4で卒業(出さない)
SRS_GRADUATE = len(SRS_INTERVALS)   # =4

def _read_review_chars():
    """review.txt の対象字(母集合)を順序つきで返す。# のコメント行は除外。"""
    if not REVIEW.exists():
        return []
    out = []
    for ln in REVIEW.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s and not s.lstrip().startswith("#") and s not in out:
            out.append(s)
    return out

def _load_srs_state():
    if SRS_STATE.exists():
        try:
            return _json.loads(SRS_STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_srs_state(state):
    SRS_STATE.write_text(_json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

def srs_mark(chars, result, today=None):
    """採点反映: result='correct'(正解)で間隔をのばす / 'miss'(まちがい)で翌日リセット。
    review.txt に無い字は自動で母集合に加える。"""
    today = today or datetime.date.today()
    BASE.mkdir(parents=True, exist_ok=True)
    state = _load_srs_state()
    existing = set(_read_review_chars())
    to_add = [c for c in chars if c and c not in existing]
    if to_add:
        with REVIEW.open("a", encoding="utf-8") as f:
            for c in to_add:
                f.write(c + "\n")
    for c in chars:
        if not c:
            continue
        st = state.get(c, {"stage": 0, "due": today.isoformat()})
        if result == "correct":
            st["stage"] = min(st.get("stage", 0) + 1, SRS_GRADUATE)
        else:  # miss
            st["stage"] = 0
        if st["stage"] >= SRS_GRADUATE:
            st["due"] = None   # 卒業: 当面出さない
        else:
            iv = SRS_INTERVALS[st["stage"]]
            st["due"] = (today + datetime.timedelta(days=iv)).isoformat()
        state[c] = st
    _save_srs_state(state)
    return state

def load_review(today=None):
    """今日の review 対象(due を迎えた字)の集合を返す。
    state に無い字は新規=即対象。卒業(due=None)済みは除外。"""
    today = today or datetime.date.today()
    chars = _read_review_chars()
    if not chars:
        return set()
    state = _load_srs_state()
    due_set = set()
    for c in chars:
        st = state.get(c)
        if st is None:
            due_set.add(c)            # 未管理=新規。すぐ出題対象
            continue
        due = st.get("due")
        if due is None:
            continue                  # 卒業済み
        try:
            if datetime.date.fromisoformat(due) <= today:
                due_set.add(c)
        except Exception:
            due_set.add(c)
    return due_set

def pick(con, cat, pool, n, today, rng, review):
    last = dict(con.execute("SELECT item,last FROM usage WHERE cat=?", (cat,)).fetchall())
    def contains_review(it):
        # E: word1/word2(ことば全体)の構成字でも照合する。
        # KC は word(空マスにする語)と other(まちがい候補)も照合対象にする。
        chars = (it.get("char","") + it.get("ans","")
                 + it.get("word1","") + it.get("word2","")
                 + it.get("word","") + it.get("other",""))
        return any(k in chars for k in review)
    # 優先度: 復習指定 > 最近出してない > ランダム
    ordered = sorted(pool, key=lambda it: (0 if contains_review(it) else 1,
                                           last.get(it["id"], 0),
                                           rng.random()))
    chosen = ordered[:n]
    for it in chosen:
        con.execute("INSERT OR REPLACE INTO usage(cat,item,last) VALUES(?,?,?)",
                    (cat, it["id"], today))
    con.commit()
    return chosen

# ── HTML 組み立て ──────────────────────────────────────
CSS = r"""
  /* 5歳向け。大きな文字・広い書きマス・なぞり見本をうすいグレーで。
     モノクロ印刷前提。色ではなく太線・濃淡・罫線で識別する。 */
  /* 字形は手書き(教科書体)に合わせる。Klee One は硬筆・教科書体寄りで
     「き」「さ」「ち」「ふ」「り」などが学校で習う筆順・字形に近い。
     ローカルにあれば優先。無ければ Google Fonts から取得する(オフラインならフォールバック)。*/
  @import url('https://fonts.googleapis.com/css2?family=Klee+One:wght@400;600&display=swap');
  @page { size: A4 portrait; margin: 0; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  /* --kana = 子どもが読む・なぞる・書く ひらがな本体に使う教科書字形フォント。
     游教科書体(YuKyokasho)を最優先。文科省学習指導要領の字形に準拠した正統な教科書体で、
     macOS には標準搭載されている。無い環境では次のフォントへ落ちる。
     以降はフォールバック: Klee → UDデジタル教科書体 → 明朝。 */
  :root { --kana: "YuKyokasho", "Yu Kyokasho", "游教科書体", "游教科書体 横用",
                   "Klee One", "Klee", "UD デジタル教科書体 NK-R", "UDデジタル教科書体 NK-R",
                   "Hiragino Mincho ProN", serif; }
  html, body { font-family: "Noto Sans CJK JP", "Hiragino Maru Gothic ProN", sans-serif;
    color: #000; background: #fff;
    -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .page { width: 210mm; height: 297mm; padding: 9mm 13mm 8mm;
    display: flex; flex-direction: column; position: relative; overflow: hidden; }
  .top { display: flex; align-items: flex-end; justify-content: space-between; }
  .kicker { font-size: 12pt; color: #000; font-weight: 800; letter-spacing:.08em; }
  h1 { font-size: 23pt; font-weight: 900; line-height: 1.2; color:#000; } h1 .n { color:#000; }
  .name { font-size: 12pt; color: #000; font-weight: 700; }
  .note { font-size: 11pt; color: #222; font-weight: 600; margin-top: 4px; line-height:1.5; }
  .rule { height: 3px; background: #000; margin: 6px 0 0; }
  /* 4セクション構成で旧サイズのままだと最終行が A4 の下端からはみ出して欠けた。セクション間隔・行の高さ・マスの寸法を一段ずつ
     詰めて11問が1ページに収まるようにしてある。ここを緩めるときは必ずPDFを生成して
     最終行が欠けていないか目視すること。 */
  .sec { margin-top: 2.5mm; }
  .shead { display: flex; align-items: center; gap: 10px; border-bottom: 2.5px solid #000; padding-bottom: 2px; }
  .stag { color:#fff; background:#000; font-weight:900; font-size:14pt; width:30px; height:30px;
          border-radius:7px; display:inline-flex; align-items:center; justify-content:center; }
  .stitle { font-size: 16pt; font-weight: 900; color:#000; white-space: nowrap; flex: 0 0 auto; }
  .ssub { font-size: 10.5pt; font-weight: 700; color:#333; margin-left: 4px; }

  /* A: なぞり書き。見本(うすいグレー)+ なぞりマス3つ + ことば */
  .arow { display:flex; align-items:center; gap:4mm; padding:1.5mm 3mm; border-bottom:1.5px dashed #999; }
  .arow:last-child{border-bottom:none;}
  .achk { width:6mm; height:6mm; flex:0 0 auto; border:2px solid #000; border-radius:2px; background:#fff; }
  .trace-box { width:15mm; height:15mm; flex:0 0 auto; border:2px solid #000; border-radius:6px;
    background-image: linear-gradient(#ccc 0 0), linear-gradient(#ccc 0 0);
    background-size: 1px 100%, 100% 1px; background-position:center; background-repeat:no-repeat;
    display:flex; align-items:center; justify-content:center; position:relative; }
  /* 見本(なぞる文字)はうすいグレーで表示。字形は教科書体(--kana)。 */
  .trace-box.sample span { font-family:var(--kana); font-size:26pt; font-weight:600; color:#cfcfcf; line-height:1; }
  .aword { font-size:13pt; font-weight:700; color:#000; margin-left:auto; }
  .aword .big { font-family:var(--kana); font-size:18pt; font-weight:600; }
  .aword .w { font-family:var(--kana); }
  /* 小書き文字(っゃゅょ)の見本は、マスの中央ではなく左下に寄せて表示する。
     横書きでは小さい字はマスの左下に書くのが決まりなので、見本の位置自体で教える。 */
  .trace-box.sk { align-items:flex-end; justify-content:flex-start; padding:0 0 1mm 1.5mm; }
  .trace-box.sk span { font-size:20pt; }
  /* 小書き文字(っゃゅょ)・ぢづ・長音ー に添える注意書き。答えではなく書き方のコツ。 */
  .anote { font-family:"Noto Sans CJK JP", sans-serif; font-size:9pt; font-weight:700;
    color:#444; display:block; text-align:right; margin-top:1px; }

  /* K: カタカナ。A と同じ「見本+なぞりマス」だが、対応するひらがなを併記し、
     マスは 15mm と一回り小さくして1行あたりの高さを抑えている(1ページに収めるため)。 */
  .krow { display:flex; align-items:center; gap:4mm; padding:1.5mm 3mm; border-bottom:1.5px dashed #999; }
  .krow:last-child{border-bottom:none;}
  .kbox { width:15mm; height:15mm; flex:0 0 auto; border:2px solid #000; border-radius:6px;
    background-image: linear-gradient(#ccc 0 0), linear-gradient(#ccc 0 0);
    background-size: 1px 100%, 100% 1px; background-position:center; background-repeat:no-repeat;
    display:flex; align-items:center; justify-content:center; }
  .kbox.sample span { font-family:var(--kana); font-size:26pt; font-weight:600; color:#cfcfcf; line-height:1; }
  /* 「ア＝あ」のように、カタカナと同じ音のひらがなを並べて対応づけさせる。 */
  .kyomi { font-family:var(--kana); font-size:13pt; font-weight:600; color:#333; flex:0 0 auto; }
  .kword { font-size:12.5pt; font-weight:700; color:#000; margin-left:auto; text-align:right; }
  .kword .big { font-family:var(--kana); font-size:17pt; font-weight:600; }
  .kword .w { font-family:var(--kana); }

  /* KC: にた字、どっち?(カタカナ)。絵 + 1字だけ空マスにした語 + 候補2字。
     候補は枠で囲んで「選ぶもの」だと一目でわかるようにする。 */
  .crow { display:flex; align-items:center; gap:3mm; padding:1.5mm 3mm; border-bottom:1.5px dashed #999; }
  .crow:last-child{border-bottom:none;}
  .cemoji { font-size:20pt; flex:0 0 auto; }
  .cword { font-family:var(--kana); font-size:17pt; font-weight:600; color:#000;
    display:flex; align-items:center; gap:1mm; flex:0 0 auto; }
  .cblank { width:13mm; height:13mm; border:2px solid #000; border-radius:5px;
    background-image: linear-gradient(#ddd 0 0), linear-gradient(#ddd 0 0);
    background-size: 1px 100%, 100% 1px; background-position:center; background-repeat:no-repeat; }
  .copts { margin-left:auto; display:flex; gap:3mm; align-items:center; }
  .clabel { font-family:"Noto Sans CJK JP", sans-serif; font-size:10pt; font-weight:700; color:#444; }
  .copt { font-family:var(--kana); font-size:19pt; font-weight:600; color:#000;
    border:2px solid #000; border-radius:6px; width:12mm; height:12mm;
    display:flex; align-items:center; justify-content:center; }

  /* E: しりとり。ことば1(絵+ことば全部見せる) → ことば2(絵だけヒント、ぜんぶ空マス)。
     word2 をぜんぶ書かせる分 1行の横幅が伸びるので、2列グリッドではなく1列にしてある。 */
  .erow { display:flex; align-items:center; gap:3mm; padding:1.5mm 3mm; border-bottom:1.5px dashed #999; }
  .erow:last-child{border-bottom:none;}
  .echk { width:6mm; height:6mm; flex:0 0 auto; border:2px solid #000; border-radius:2px; background:#fff; }
  .eemoji { font-size:22pt; flex:0 0 auto; }
  .eword { font-family:var(--kana); font-size:15pt; font-weight:600; color:#000; flex:0 0 auto; }
  .earrow { font-size:15pt; font-weight:900; color:#000; flex:0 0 auto; }
  /* emoji2だけでは連想しにくい語に添える短いヒント文字(答えの字は含まない)。 */
  .ehint { font-family: "Noto Sans CJK JP", sans-serif; font-size:9pt; font-weight:700;
    color:#555; flex:0 0 auto; margin-right:1mm; }
  .ebgroup { display:flex; gap:1.5mm; }
  .ebox { width:13mm; height:13mm; border:2px solid #000; border-radius:5px;
    background-image: linear-gradient(#ddd 0 0), linear-gradient(#ddd 0 0);
    background-size: 1px 100%, 100% 1px; background-position:center; background-repeat:no-repeat; }

  /* KC/KE: ひらがなの読み。絵の下に添えて「よんで→カタカナで かく」にする。 */
  .cpic, .epic { display:flex; flex-direction:column; align-items:center; flex:0 0 auto; line-height:1; }
  .cpic .cemoji, .epic .eemoji { line-height:1.05; }
  .hyomi { font-family:var(--kana); font-size:12.5pt; font-weight:600; color:#222; white-space:nowrap;
    margin-top:0.5mm; }
  /* KE ことば1: カタカナの上に ひらがなの ふりがな(ruby)。行高を増やさないよう小さめ。 */
  .eword ruby { ruby-position: over; }
  .eword rt { font-family:var(--kana); font-size:8pt; font-weight:600; color:#333; }

  /* 答え欄 */
  .akey { margin-top: 5mm; background:#fff; border:2.5px solid #000; border-radius:10px; padding:5mm 6mm; }
  .akey h2 { font-size:14pt; color:#000; font-weight:900; margin-bottom:5px;}
  .akey .row { font-family:var(--kana); font-size:14pt; font-weight:600; line-height:1.9; color:#000; }
  .akey .row .lbl { font-family:"Noto Sans CJK JP", sans-serif; }
  .akey .lbl { display:inline-block; width:1.7em; color:#000; font-weight:900; } .akey .ans { color:#000; font-weight:900; }
  .parent { margin-top:auto; background:#f0f0f0; border:2.5px solid #000; border-radius:10px; padding:5mm 6mm; }
  .parent h3{ font-size:12pt; color:#000; font-weight:900; margin-bottom:4px;}
  .parent p{ font-size:12pt; font-weight:600; color:#111; line-height:1.7;}
  .foot { font-size:10pt; color:#666; text-align:right; margin-top:5px;}
"""

# 紙面の見出しに使う全角アルファベット。出題のあるセクションだけで先頭から順に消費する。
SEC_LETTERS = ["Ａ", "Ｂ", "Ｃ", "Ｄ", "Ｅ", "Ｆ"]

def kata2hira(word, override=None):
    """カタカナ語の ひらがな読み。絵(emoji)だけでは何の語か分かりにくい
    (例: 🧺=タオル, 🛋️=ソース)ため、KC/KE では ひらがなの読みを併記して
    「ひらがなを読む → 音を取る → カタカナで書く」の流れにした。長音ー は ひらがな側でも ー のまま。
    プール項目に yomi(KC) / yomi1・yomi2(KE) があればそれを優先する。"""
    if override:
        return override
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in word)


def html_doc(body):
    return f'<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8"><style>{CSS}</style></head><body>{body}</body></html>'

def _title():
    return f"{CHILD}の カタカナ" if CHILD else "カタカナ "

def build_problem(date, A, E, K, KC, KE, rng):
    d = f"{date.month}/{date.day}"
    title = _title()
    age_html = f"（{AGE}さい）" if AGE else ""
    # A: なぞり書き。1つ目のマスは見本(うすいグレー)、残り2つは空マス。
    def arow(it):
        word = rng.choice(it["words"])
        sk = " sk" if it["char"] in SMALL_KANA else ""
        sample = f'<div class="trace-box sample{sk}"><span>{it["char"]}</span></div>'
        blanks = '<div class="trace-box"></div><div class="trace-box"></div>'
        note = it.get("note", "")
        note_html = f'<span class="anote">{note}</span>' if note else ""
        return (f'<div class="arow"><div class="achk"></div>{sample}{blanks}'
                f'<div class="aword"><span class="big">{it["char"]}</span>　<span class="w">{word}</span>'
                f'{note_html}</div></div>')
    rows_a = "".join(arow(it) for it in A)
    # K: カタカナ。見本1マス+空マス2つ。対応するひらがなを「＝あ」の形で併記する。
    def krow(it):
        word = rng.choice(it["words"])
        sample = f'<div class="kbox sample"><span>{it["char"]}</span></div>'
        blanks = '<div class="kbox"></div><div class="kbox"></div>'
        note = it.get("note", "")
        note_html = f'<span class="anote">{note}</span>' if note else ""
        return (f'<div class="krow"><div class="achk"></div>{sample}{blanks}'
                f'<span class="kyomi">＝{it["yomi"]}</span>'
                f'<div class="kword"><span class="big">{it["char"]}</span>　<span class="w">{word}</span>'
                f'{note_html}</div></div>')
    rows_k = "".join(krow(it) for it in K)
    # E: しりとり。ことば1は絵+ことば全部を見せ、ことば2は絵だけヒントに出して
    # マスを全部空欄にする(以前は先頭1文字だけ空欄で写すだけだった)。
    def erow(it, kata=False):
        boxes = "".join('<div class="ebox"></div>' for _ in it["word2"])
        hint = it.get("hint2", "")
        hint_html = f'<span class="ehint">({hint})</span>' if hint else ""
        if kata:
            # KE(カタカナ): ことば1 には ひらがなの ふりがな、ことば2 には
            # 絵の下に ひらがなの読み を出す。絵だけでは連想しにくい語が多かったため。
            # 「ひらがなを読む → カタカナで書く」の練習に寄せる。
            y1 = kata2hira(it["word1"], it.get("yomi1"))
            y2 = kata2hira(it["word2"], it.get("yomi2"))
            w1 = f'<span class="eword"><ruby>{it["word1"]}<rt>{y1}</rt></ruby></span>'
            pic2 = f'<div class="epic"><span class="eemoji">{it["emoji2"]}</span><span class="hyomi">{y2}</span></div>'
        else:
            w1 = f'<span class="eword">{it["word1"]}</span>'
            pic2 = f'<span class="eemoji">{it["emoji2"]}</span>'
        return (f'<div class="erow"><div class="echk"></div>'
                f'<span class="eemoji">{it["emoji1"]}</span>'
                f'{w1}'
                f'<span class="earrow">→</span>'
                f'{pic2}{hint_html}'
                f'<div class="ebgroup">{boxes}</div></div>')
    rows_e  = "".join(erow(it) for it in E)
    rows_ke = "".join(erow(it, kata=True) for it in KE)
    # KC: にた字、どっち?。語の1字だけを空マスにし、右に候補2字を出す。
    # 候補の左右は rng でシャッフルして、位置で答えを覚えられないようにする。
    def crow(it):
        ans = it["word"][it["idx"]]
        parts = []
        for i, ch in enumerate(it["word"]):
            if i == it["idx"]:
                parts.append('<div class="cblank"></div>')
            else:
                parts.append(f"<span>{ch}</span>")
        opts = [ans, it["other"]]
        rng.shuffle(opts)
        opt_html = "".join(f'<div class="copt">{o}</div>' for o in opts)
        yomi = kata2hira(it["word"], it.get("yomi"))
        return (f'<div class="crow"><div class="achk"></div>'
                f'<div class="cpic"><span class="cemoji">{it["emoji"]}</span><span class="hyomi">{yomi}</span></div>'
                f'<div class="cword">{"".join(parts)}</div>'
                f'<div class="copts"><span class="clabel">どっち?</span>{opt_html}</div></div>')
    rows_kc = "".join(crow(it) for it in KC)
    # 出題数0のセクションは描画しない。見出しの文字は「出るセクションだけ」で Ａ→Ｂ→Ｃ→Ｄ の順に
    # 詰めて振り直される。通常は
    # Ａ=カタカナを なぞろう / Ｂ=にた字、どっち? / Ｃ=カタカナ しりとり / Ｄ=ひらがな ふくしゅう。
    # ひらがなが全字卒業すると Ｄ が消えて3セクションになる。
    # **採点の読み取りでは見出しのアルファベットではなくセクションタイトルの文言で判定すること。**
    secs = []
    if K:  secs.append(("カタカナを なぞろう", "うすい字を なぞって、となりに まねして かこう", f"<div>{rows_k}</div>"))
    if KC: secs.append(("にた字、どっち?", "ひらがなを よんで、あう カタカナを えらんで □に かこう", f"<div>{rows_kc}</div>"))
    if KE: secs.append(("カタカナ しりとり", "①のさいごの字で はじまる②を、ひらがなを よんで カタカナで かこう", f"<div>{rows_ke}</div>"))
    if A:  secs.append(("ひらがな ふくしゅう", "うすい字を なぞって、となりに まねして かこう", f"<div>{rows_a}</div>"))
    if E:  secs.append(("ひらがな しりとり", "①のさいごの字から始まる②を、えをヒントに ぜんぶ かこう", f"<div>{rows_e}</div>"))
    sec_html = "".join(
        f'<div class="sec"><div class="shead"><span class="stag">{lt}</span>'
        f'<span class="stitle">{t}</span><span class="ssub">{s}</span></div>{inner}</div>'
        for lt, (t, s, inner) in zip(SEC_LETTERS, secs))
    body = f'''
    <div class="page">
      <div class="top"><div>
        <div class="kicker">カタカナ れんしゅう{age_html}</div>
        <h1>{title}<span class="n">ドリル</span></h1>
      </div><div class="name">なまえ：{CHILD or "　　　　"}　ひづけ：{d}</div></div>
      <div class="note">ゆっくりで いいよ。わからない もんだいは ひだりの □ を ぬろう。<br/>★ おうちの方へ：なぞる→まねして かく の順で。声に出すと もっと おぼえます。</div>
      <div class="rule"></div>
      {sec_html}
    </div>'''
    return html_doc(body)

def build_answer(date, A, E, K, KC, KE):
    d = f"{date.month}/{date.day}"
    title = _title()
    a = "　".join(f'<span class="lbl">{i}</span><span class="ans">{it["char"]}</span>'
                  for i, it in enumerate(A, 1))
    e = "　".join(f'<span class="lbl">{i}</span><span class="ans">{it["word1"]}→{it["word2"]}</span>'
                  for i, it in enumerate(E, 1))
    ke = "　".join(f'<span class="lbl">{i}</span><span class="ans">{it["word1"]}→{it["word2"]}</span>'
                  for i, it in enumerate(KE, 1))
    k = "　".join(f'<span class="lbl">{i}</span><span class="ans">{it["char"]}（{it["yomi"]}）</span>'
                  for i, it in enumerate(K, 1))
    kc = "　".join(f'<span class="lbl">{i}</span><span class="ans">{it["word"][it["idx"]]}（{it["word"]}）</span>'
                  for i, it in enumerate(KC, 1))
    tip_e = ('<div class="row" style="font-size:11.5pt;">※ ①のことばを 声に出して さいごの音を 確認してから、'
             'その字で②のことばが始まっているか、②のマスが全部うまっているかを見てあげて。'
             '1文字でもちがっていたら その行の □ を ぬってください。</div>')
    tip_ke = ('<div class="row" style="font-size:11.5pt;">※ ①のことばを 声に出して さいごの音を 確認してから、'
              'その字で②のことばが始まっているか、②のマスが全部うまっているかを見てあげて。'
              'カタカナの しりとりは 音が とれていれば まず ○ に。'
              '字形の くずれではなく、ちがう字を 書いている ときだけ □ を ぬってください。</div>')
    tip_kc = ('<div class="row" style="font-size:11.5pt;">※ シとツ・ソとン・クとワ など、'
              'カタカナで いちばん まちがえやすい 組を 出しています。'
              '点の むきや はらいの むきが 逆になっていたら、選んだ字が 合っていても □ を ぬって、'
              'その字を もう一度 なぞらせてあげてください。</div>')
    # 小書き文字(っゃゅょ)は「マスの左下に小さく書けているか」まで見ないと意味がないので、
    # A に小書き文字が出た日は 答え票にも明示的に注意を出す。
    small = [it["char"] for it in A if it["char"] in "っゃゅょ"]
    tip_a = ('<div class="row" style="font-size:11.5pt;">※ 小さい字（'
             + "・".join(small) +
             '）は マスの ひだりした に ちいさく 書けているかを見てあげて。'
             '大きさが ふつうの字と同じになっていたら まちがい扱いで □ を ぬってください。</div>'
             ) if small else ""
    tip_k = ('<div class="row" style="font-size:11.5pt;">※ カタカナの なぞりは 形も 見てあげて ください。'
             'ひらがなと 音が むすびついていない とき、'
             'または 形が 大きく くずれている ときに □ を ぬってください。</div>')
    keys = []
    if K:  keys.append(("カタカナを なぞろう", k,  tip_k))
    if KC: keys.append(("にた字、どっち?", kc, tip_kc))
    if KE: keys.append(("カタカナ しりとり", ke, tip_ke))
    if A:  keys.append(("ひらがな ふくしゅう", a, tip_a))
    if E:  keys.append(("ひらがな しりとり", e, tip_e))
    key_html = "".join(
        f'<div class="akey"><h2>{lt}　{t}</h2><div class="row">{row}</div>{tip}</div>'
        for lt, (t, row, tip) in zip(SEC_LETTERS, keys))
    body = f'''
    <div class="page">
      <div class="top"><div><div class="kicker">★ おうちの かたへ</div>
      <h1>こたえ <span class="n">＆ まるつけ</span></h1></div>
      <div class="name">{title}ドリル　{d}</div></div>
      <div class="rule"></div>
      {key_html}
      <div class="parent"><h3>まるつけの あとに</h3>
        <p>×だった字は <code>srs-mark miss</code> で伝えると（review.txt に1行ずつ足しても同じ）、次の日から その字を ねらって出します。きれいに 書けなくても、なぞれた・声に 出せたら はなまる。できた数を 一緒に 数えて ほめてあげてください。</p></div>
      <div class="foot">{title}ドリル ／ こたえ ／ {d}</div>
    </div>'''
    return html_doc(body)

# ── 描画 ───────────────────────────────────────────────
# フック: 渡す用 PDF の HTML を描画前に加工したいとき（スキャン仕分け用の目印など）はここに足す。
def _pre_render_hook(html, out_path):
    return html


def render(html, out_path, html_only=False):
    html = _pre_render_hook(html, out_path)
    if html_only:
        out_html = pathlib.Path(str(out_path)).with_suffix(".html")
        out_html.write_text(html, encoding="utf-8")
        return out_html
    from playwright.sync_api import sync_playwright  # PDF生成時のみ必要(遅延import)
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html); tmp = f.name
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page()
        # networkidle まで待つことで Google Fonts(Klee One)の取得完了を待つ
        pg.goto("file://" + tmp, wait_until="networkidle")
        # 念のためフォント読み込み完了を明示的に待機
        try:
            pg.evaluate("document.fonts.ready")
            pg.wait_for_function("document.fonts.status === 'loaded'", timeout=8000)
        except Exception:
            pass  # フォント未取得でもローカルフォールバックで描画は続行
        pg.pdf(path=str(out_path), width="210mm", height="297mm",
               print_background=True, margin={"top":"0","bottom":"0","left":"0","right":"0"})
        b.close()
    os.unlink(tmp)
    return out_path

# ── 問題の自動ダブルチェック ────────────────────────────────
# 語頭に立てない字。"その字で始まる語" ではなく "その字を含む語" で検証する。
# を(助詞)・小書き文字(っゃゅょ)・四つ仮名(ぢづ)・長音(ー)が該当する。
CONTAINS_CHARS = set("をっゃゅょぢづー")
# 小書き文字。横書きではマスの左下に小さく書く。見本の表示位置もそれに合わせる。
SMALL_KANA = set("っゃゅょ")
# 語頭に立てず、語末にしか現れない字。末尾一致で検証する。
ENDSWITH_CHARS = set("んン")

def _check_a(pool, name):
    errs = []; seen = set()
    for it in pool:
        i = it.get("id", "?")
        if i in seen: errs.append(f"[{name}] id重複: {i}")
        seen.add(i)
        char = it.get("char", "")
        if not char: errs.append(f"[{name}] {i}: char が空")
        words = it.get("words") or []
        if not words: errs.append(f"[{name}] {i}: words が空")
        for w in words:
            if char in CONTAINS_CHARS:
                if char not in w: errs.append(f"[{name}] {i}: word'{w}'に'{char}'が含まれていない")
            elif char in ENDSWITH_CHARS:
                if not w.endswith(char): errs.append(f"[{name}] {i}: word'{w}'が'{char}'で終わっていない")
            elif not w.startswith(char):
                errs.append(f"[{name}] {i}: word'{w}'が'{char}'で始まっていない")
    return errs

def _check_k(pool, name):
    """K(カタカナ)は _check_a の検証に加えて、yomi(対応するひらがな)の有無と
    語がカタカナ表記になっているかを見る。"""
    errs = _check_a(pool, name)
    for it in pool:
        i = it.get("id", "?")
        if not it.get("yomi"): errs.append(f"[{name}] {i}: yomi(対応するひらがな)が空")
        for w in it.get("words") or []:
            if any("ぁ" <= ch <= "ゖ" for ch in w):
                errs.append(f"[{name}] {i}: word'{w}'にひらがなが混ざっている")
    return errs

def _check_e(pool, name):
    errs = []; seen = set()
    for it in pool:
        i = it.get("id", "?")
        if i in seen: errs.append(f"[{name}] id重複: {i}")
        seen.add(i)
        if not it.get("emoji1"): errs.append(f"[{name}] {i}: emoji1 が空")
        if not it.get("emoji2"): errs.append(f"[{name}] {i}: emoji2 が空")
        ans = it.get("ans","")
        w1, w2 = it.get("word1",""), it.get("word2","")
        if not ans: errs.append(f"[{name}] {i}: ans が空")
        elif w1 and not w1.endswith(ans):
            errs.append(f"[{name}] {i}: ans'{ans}'が word1'{w1}'の末尾と一致しない")
        elif w2 and not w2.startswith(ans):
            errs.append(f"[{name}] {i}: ans'{ans}'が word2'{w2}'の先頭と一致しない")
        # word2 を全部自分で書かせる形式なので、マスの横幅がはみ出ないよう
        # 2〜4文字に収まっているかをチェックする(word1は読むだけなので制約なし)。
        if not (2 <= len(w2) <= 4):
            errs.append(f"[{name}] {i}: word2'{w2}'は2〜4文字にする(マスの横幅の制約)")
        hint2 = it.get("hint2", "")
        if hint2 and ans and ans in hint2:
            errs.append(f"[{name}] {i}: hint2'{hint2}'に答えの字'{ans}'が含まれている")
    return errs

def _check_kc(pool, name):
    """KC(にた字、どっち?)の検証。idx が語の範囲に収まっているか、正解と まちがい候補が
    別の字か、語と候補がカタカナだけで書かれているかを見る。"""
    errs = []; seen = set()
    for it in pool:
        i = it.get("id", "?")
        if i in seen: errs.append(f"[{name}] id重複: {i}")
        seen.add(i)
        if not it.get("emoji"): errs.append(f"[{name}] {i}: emoji が空")
        w, idx, other = it.get("word", ""), it.get("idx", -1), it.get("other", "")
        if not w: errs.append(f"[{name}] {i}: word が空"); continue
        if not (0 <= idx < len(w)):
            errs.append(f"[{name}] {i}: idx={idx} が word'{w}'の範囲外"); continue
        if not other: errs.append(f"[{name}] {i}: other(まちがい候補)が空")
        elif len(other) != 1: errs.append(f"[{name}] {i}: other'{other}'は1字にする")
        elif other == w[idx]: errs.append(f"[{name}] {i}: other'{other}'が正解と同じ字")
        # 語が長すぎると候補ボックスと衝突する。空マス込みで5字まで。
        if len(w) > 5: errs.append(f"[{name}] {i}: word'{w}'は5文字までにする(紙面の横幅の制約)")
        for ch in w + other:
            if "ぁ" <= ch <= "ゖ":
                errs.append(f"[{name}] {i}: '{ch}'がひらがなになっている(カタカナで書く)")
    return errs

# しりとりの1つ目の語に使ってはいけない末尾の字。ン は次に続けられず、長音ー は
# 「のばす音の前の母音を取る」処理が5歳には難しく、小書きは そもそも語頭に立てない。
KE_BAD_TAIL = set("ンーャュョッァィゥェォ")

def _check_ke(pool, name):
    """KE(カタカナ しりとり)の検証。_check_e の共通チェックに加えて、
    word1 の末尾が しりとりを続けられる字か、語がカタカナだけかを見る。"""
    errs = _check_e(pool, name)
    for it in pool:
        i = it.get("id", "?")
        w1, w2 = it.get("word1", ""), it.get("word2", "")
        if w1 and w1[-1] in KE_BAD_TAIL:
            errs.append(f"[{name}] {i}: word1'{w1}'が'{w1[-1]}'で終わっている"
                        f"(しりとりが続かないので1つ目の語には使わない)")
        # word2 は子どもが自分で書くので、まだ教えていない小書きカタカナは入れない。
        for ch in w2:
            if ch in "ャュョッァィゥェォ":
                errs.append(f"[{name}] {i}: word2'{w2}'に小書きの'{ch}'が入っている"
                            f"(カタカナの小書きは未習のため答えには使わない)")
        for ch in w1 + w2:
            if "ぁ" <= ch <= "ゖ":
                errs.append(f"[{name}] {i}: '{ch}'がひらがなになっている(カタカナで書く)")
    return errs

def validate_pools():
    errs = []
    errs += _check_a(POOL_A, "POOL_A")
    errs += _check_e(POOL_E, "POOL_E")
    errs += _check_k(POOL_K, "POOL_K")
    errs += _check_kc(POOL_KC, "POOL_KC")
    errs += _check_ke(POOL_KE, "POOL_KE")
    return errs

# ── メイン ─────────────────────────────────────────────
def _srs_cli():
    """SRS操作サブコマンド。PDFを作らず state を更新/表示して終了する。
    使い方:
      kana_drill.py srs-mark correct さ し す   # 正解→間隔をのばす
      kana_drill.py srs-mark miss の は         # まちがい→翌日リセット
      kana_drill.py srs-status                  # いまの due 状況を表示
    戻り値: 処理したら True(=ここで終了), サブコマンドでなければ False。"""
    if len(sys.argv) < 2:
        return False
    sub = sys.argv[1]
    if sub == "srs-mark":
        result = sys.argv[2] if len(sys.argv) > 2 else ""
        # 「のは」のように続けて渡されても1字ずつに分解する(キーは常に1字)
        chars = [c for arg in sys.argv[3:] for c in arg.strip() if c.strip()]
        if result not in ("correct", "miss") or not chars:
            print("usage: srs-mark <correct|miss> 字 字 ...", file=sys.stderr); sys.exit(2)
        state = srs_mark(chars, result)
        for c in chars:
            st = state.get(c, {})
            due = st.get("due") or "卒業"
            print(f"  {c}: stage={st.get('stage')} 次回={due}")
        print(f"{result}: {len(chars)}字を更新しました")
        return True
    if sub == "srs-status":
        today = datetime.date.today()
        chars = _read_review_chars(); state = _load_srs_state()
        due = sorted(load_review(today))
        grad = [c for c in chars if (state.get(c) or {}).get("due") is None and c in state]
        print(f"review対象 合計 {len(chars)}字 / 今日出す(due) {len(due)}字 / 卒業 {len(grad)}字")
        print("今日のreview対象:", "".join(due) if due else "(なし)")
        if grad:
            print("卒業した字:", "".join(grad))
        return True
    return False

def main():
    if _srs_cli():
        return
    import argparse
    ap = argparse.ArgumentParser(description="毎日1ページのかなドリルを生成する")
    ap.add_argument("date", nargs="?", help="対象日 YYYY-MM-DD（省略時は今日）")
    ap.add_argument("--date", dest="date_opt", help="対象日 YYYY-MM-DD")
    ap.add_argument("--html-only", action="store_true", help="PDF を作らず HTML だけ出す（playwright 不要）")
    ap.add_argument("--out", help="出力先ディレクトリ（既定: $KANA_DRILL_DIR/out）")
    args = ap.parse_args()
    global OUTDIR
    if args.out:
        OUTDIR = pathlib.Path(args.out).expanduser()
    date_s = args.date_opt or args.date
    try:
        date = datetime.date.fromisoformat(date_s) if date_s else datetime.date.today()
    except ValueError:
        ap.error(f"日付は YYYY-MM-DD で指定してください: {date_s}")

    problems = validate_pools()
    if problems:
        print("[ERROR] ドリル問題の検証に失敗しました。生成を中止します:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        sys.exit(2)

    rng  = random.Random(int(date.strftime("%Y%m%d")))
    # 生成対象日を渡し、「その日に due を迎える字」で出題する（未来日のドリルを作るとき用）
    review = load_review(date)
    con  = db_conn()
    # ひらがなの A は「SRS が due と判定した未卒業字」だけを母集合にする。
    # 全字が stage4 に到達すると母集合が空になり、A セクション自体が紙面から消える。
    pool_a_due = [it for it in POOL_A if it["char"] in review]
    A  = pick(con, "A",  pool_a_due,     N_A,  date.toordinal(), rng, review)
    E  = pick(con, "E",  list(POOL_E),   N_E,  date.toordinal(), rng, review)
    K  = pick(con, "K",  list(POOL_K),   N_K,  date.toordinal(), rng, review)
    KC = pick(con, "KC", list(POOL_KC),  N_KC, date.toordinal(), rng, review)
    KE = pick(con, "KE", list(POOL_KE),  N_KE, date.toordinal(), rng, review)
    con.close()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    stem = OUTDIR / f"kana_drill_{date.isoformat()}{OUT_SUFFIX}"
    p1 = render(build_problem(date, A, E, K, KC, KE, rng), f"{stem}.pdf", args.html_only)
    p2 = render(build_answer(date, A, E, K, KC, KE),       f"{stem}_answer.pdf", args.html_only)
    print(f"渡す用 : {p1}")
    print(f"答え   : {p2}")

if __name__ == "__main__":
    main()
