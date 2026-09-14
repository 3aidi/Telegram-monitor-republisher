"""Country -> Telegram custom emoji mapping and flag rendering.

Single static source of truth for country flags rendered as REAL Telegram
custom emoji. Country names map to the document_id of their custom emoji.
Every flag is anchored in the message text on the EXACT standard emoji that
the custom document's ``documentAttributeCustomEmoji.alt`` contains (e.g. the
real 🇵🇱 glyph for Poland) and wrapped by a MessageEntityCustomEmoji entity.
This anchor MUST be that exact emoji — Telegram only renders a custom emoji
entity when "this entity must wrap exactly one regular emoji (the one
contained in documentAttributeCustomEmoji.alt) in the related text, otherwise
the server will ignore it". A generic placeholder ("·", "_") is silently
dropped and shows up as literal text in the published post.

Two packs are supported:
  * COUNTRY_EMOJI          - the active map (currently FlagsEmoji2024).
  * FLAGS2024_DUMP         - when pasted (raw "@TgEmojis / Get Emoji ID Bot"
    scrape of the FlagsEmoji2024 premium pack), each "N)FLAG [document_id]"
    line is decoded via the flag's Unicode regional-indicator code points into
    the matching ISO 3166-1 country name, and COUNTRY_EMOJI is REPLACED with
    the new pack's document_ids. The row's flag glyph also becomes the alt
    anchor (``_FLAG_ALTS``). Nothing is ever guessed: an unrecognized flag
    glyph is skipped, never mapped.

However country flags reach a published post, the SAME rules hold:
  * a country mentioned in a body line gets its flag appended to that SAME
    line (after sanitization, so the sanitizer can never strip it);
  * a country in the raw source that the body does NOT already mention gets
    its own generated "NAME <flag>" line below the body;
  * a country already represented in the body is never re-emitted -> no
    duplicate "NO POLAND / POLAND <flag>" leak (the BUG this module fixes);
  * a country with no mapping stays plain text (no wrong flag is ever guessed).

Add or edit a country in COUNTRY_EMOJI (or an alias in _COUNTRY_ALIASES) and
every published post picks it up automatically.
"""

import logging
import re
import unicodedata
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("countries")

# Canonical country/region name -> custom emoji document_id (Emoji Flags
# @Sticker_Awwww). Names not listed here are simply not emitted (no lookup, no
# fallback fetch). This is the ACTIVE pack until FLAGS2024_DUMP is pasted.
COUNTRY_EMOJI: Dict[str, int] = {
    "Afghanistan": 5291937511591925566,
    "Åland Islands": 5294077418917616055,
    "Albania": 5294202819077756005,
    "Algeria": 5294048127240655242,
    "American Samoa": 5291994273879709721,
    "Andorra": 5294215205763434181,
    "Angola": 5294516785482062829,
    "Anguilla": 5292186323342350940,
    "Antigua and Barbuda": 5294005972136647964,
    "Argentina": 5292208210495689627,
    "Armenia": 5291978717508164018,
    "Aruba": 5294007002928798927,
    "Australia": 5294444247779399477,
    "Austria": 5291975174160145850,
    "Azerbaijan": 5294323533428579078,
    "Bahamas": 5294031587321600012,
    "Bahrain": 5294108398516720753,
    "Bangladesh": 5291824687096027834,
    "Barbados": 5294526187165471742,
    "Belarus": 5294134426018536120,
    "Belgium": 5291774466043435275,
    "Belize": 5294171848068584842,
    "Benin": 5293984969746566866,
    "Bhutan": 5294121983498277263,
    "Bolivia": 5294201479047957700,
    "Botswana": 5294026179957772585,
    "Brazil": 5291892229751723900,
    "Brunei": 5292098293692650297,
    "Bulgaria": 5294308947719640437,
    "Burkina Faso": 5294153164960848949,
    "Burundi": 5294051631933967760,
    "Cambodia": 5294225191562400452,
    "Cameroon": 5291997306126626950,
    "Canada": 5292290347450259214,
    "Cape Verde": 5292203503211535593,
    "Central African Republic": 5294210571493724819,
    "Chad": 5291780728105753403,
    "Chile": 5294231037012888049,
    "China": 5294068833277990704,
    "Colombia": 5294010206974397371,
    "Comoros": 5294351381996521508,
    "Congo": 5294035229453865597,
    "Cook Islands": 5292098684534675100,
    "Costa Rica": 5292063805105263554,
    "Côte d'Ivoire": 5293991322003200135,
    "Croatia": 5291999676948569127,
    "Cuba": 5291963947115631526,
    "Cyprus": 5294062721539526918,
    "Czechia": 5294242852467923382,
    "Denmark": 5294531860817268837,
    "Djibouti": 5294127214768468283,
    "Dominica": 5294485513825178032,
    "Dominican Republic": 5294522197140857947,
    "Ecuador": 5292083733753517221,
    "Egypt": 5293992082212409502,
    "El Salvador": 5294337307388695687,
    "Equatorial Guinea": 5292170045416297012,
    "Eritrea": 5291922054004625949,
    "Estonia": 5291951143818123103,
    "Ethiopia": 5292245976143124155,
    "European Union": 5291992809295861098,
    "Gibraltar": 5292055799286224027,
    "Gambia": 5294399820637688352,
    "Greenland": 5292014752283774878,
    "Finland": 5294049961191690629,
    "France": 5291817660529533837,
    "Gabon": 5294321325815389139,
    "Georgia": 5294349389131697267,
    "Germany": 5292013274815028523,
    "Ghana": 5294347396266873249,
    "Greece": 5291948395039054764,
    "Guinea-Bissau": 5294409819321550432,
    "Guatemala": 5294336633078831209,
    "Guinea": 5291892096607739008,
    "Guyana": 5292062692708736193,
    "Haiti": 5292045130587462814,
    "Honduras": 5291901034434682297,
    "Hong Kong": 5292166459118606932,
    "Hungary": 5294229581018975260,
    "Iceland": 5294354358408859664,
    "India": 5291933173674957761,
    "Iran": 5294220170745630736,
    "Iraq": 5294325010897327367,
    "Ireland": 5294471971793293647,
    "Isle of Man": 5294318478252070646,
    "Israel": 5294069056616289553,
    "Italy": 5291826830284709120,
    "Jamaica": 5294505107465982830,
    "Japan": 5291799063321139445,
    "Jersey": 5291950280529697493,
    "Jordan": 5291988613112814801,
    "Kazakhstan": 5294227175837290463,
    "Kenya": 5292111852904416801,
    "Kiribati": 5294538934628405146,
    "North Korea": 5294193812531333564,
    "South Korea": 5294408281723262763,
    "Kuwait": 5292066437920218075,
    "Kyrgyzstan": 5292091954320922577,
    "Laos": 5291981530711746037,
    "Latvia": 5292236016113966127,
    "Lebanon": 5294193108156699621,
    "Lesotho": 5292040693886247604,
    "Liberia": 5291793810576137439,
    "Libya": 5291858711826946840,
    "Liechtenstein": 5292048742654957785,
    "Lithuania": 5294343084119708700,
    "Luxembourg": 5294423709245787718,
    "North Macedonia": 5294023611567332075,
    "Madagascar": 5291991568050312348,
    "Malawi": 5294241881805312589,
    "Malaysia": 5291858351049696702,
    "Maldives": 5292004203844097218,
    "Mali": 5292086972158858331,
    "Malta": 5294532213004588353,
    "Marshall Islands": 5294180730060954484,
    "Mauritania": 5294429743674840973,
    "Mauritius": 5294127824653797277,
    "Mexico": 5294535073452809778,
    "Micronesia": 5291838156113470124,
    "Moldova": 5294158486425325375,
    "Monaco": 5294378161117614233,
    "Mongolia": 5294316532631883496,
    "Morocco": 5292108962391414885,
    "Mozambique": 5294086708931874940,
    "Myanmar": 5294254478944393569,
    "Namibia": 5292021761670404922,
    "Nauru": 5294463274484521342,
    "Nepal": 5294458756178924088,
    "Netherlands": 5291917797692042265,
    "New Zealand": 5294189019347833274,
    "Nicaragua": 5294240825243358100,
    "Niger": 5291809418487290691,
    "Nigeria": 5294456308047563965,
    "Niue": 5294471336138134209,
    "Norway": 5291761718580502030,
    "Oman": 5291813666209946812,
    "Pakistan": 5291825606219029010,
    "Palestine": 5294289826525238172,
    "Panama": 5291959935616178405,
    "Papua New Guinea": 5291917995260533077,
    "Paraguay": 5294525611639852679,
    "Philippines": 5291798075478661634,
    "Peru": 5292099427564018941,
    "Poland": 5292190970496963836,
    "Portugal": 5294436555492973610,
    "Puerto Rico": 5292121516580820347,
    "Qatar": 5292166360334357676,
    "Romania": 5294107724206856227,
    "Russia": 5294335323113807278,
    "Rwanda": 5294191265615729158,
    "San Marino": 5292147350809106831,
    "São Tomé and Príncipe": 5292183188016222701,
    "Saudi Arabia": 5294163983983463099,
    "Scotland": 5294434665707368018,
    "Senegal": 5292087023698466689,
    "Serbia": 5294458584380230360,
    "Seychelles": 5291891186074672309,
    "Sierra Leone": 5294494314213167952,
    "Singapore": 5294451304410663668,
    "Slovakia": 5294538440707166931,
    "Slovenia": 5294279359689938006,
    "Solomon Islands": 5294283890880433237,
    "Somalia": 5294058817414255960,
    "South Africa": 5294325281480266304,
    "Spain": 5294513087515216901,
    "Sri Lanka": 5292102670264328257,
    "Sudan": 5294177148058228060,
    "Suriname": 5294396668131692138,
    "Eswatini": 5294312482477724867,
    "Sweden": 5291737091238026321,
    "Switzerland": 5291791748991835084,
    "Syria": 5294013428199869487,
    "Taiwan": 5294095745543069603,
    "Tajikistan": 5294120269806328883,
    "Tanzania": 5292146096678658977,
    "Thailand": 5293994384314882755,
    "Togo": 5294097669688415562,
    "Tonga": 5294283689016973348,
    "Trinidad and Tobago": 5294362935458548705,
    "Tunisia": 5294484680601521871,
    "Turkey": 5293993400767367408,
    "Turkmenistan": 5294098958178603764,
    "Turks and Caicos Islands": 5294320866253884749,
    "United States": 5294244076533600593,
    "Uganda": 5294192317882716626,
    "United Arab Emirates": 5294314831824835370,
    "United Kingdom": 5293993521026453119,
    "Ukraine": 5294263837678131580,
    "Vanuatu": 5294448585696368047,
    "Uzbekistan": 5294217645304864345,
    "Uruguay": 5291928449210932974,
    "Venezuela": 5294476442854247878,
    "Vietnam": 5294235963340379688,
    "US Virgin Islands": 5294228039125718124,
    "Wales": 5294139949346476093,
    "Yemen": 5294058972033076492,
    "Zambia": 5294100109229838880,
    "Zimbabwe": 5294422158762592930,
}

# ---------------------------------------------------------------------------
# FlagsEmoji2024 premium pack (raw scrape, "@TgEmojis / Get Emoji ID Bot").
#
# Paste the full dump below in the same format it was scraped:
#     1)🇦🇫 [5291...]
#     2)🏴󠁧󠁢󠁥󠁮󠁧󠁿 [5294...]
#     63)🇪🇺 [5291...]
# Each flag's country is DERIVED from its Unicode regional-indicator code
# points (ISO 3166-1 alpha-2), never hand-typed. Once pasted, COUNTRY_EMOJI is
# replaced with these document_ids on the next import and every published post
# switches packs automatically. An empty dump keeps the legacy pack above.
# ---------------------------------------------------------------------------
FLAGS2024_DUMP: str = """1)🚩 [5294236848103643477]
2)🇦🇫 [5291937511591925566]
3)🇦🇽 [5294077418917616055]
4)🇦🇱 [5294202819077756005]
5)🇩🇿 [5294048127240655242]
6)🇦🇸 [5291994273879709721]
7)🇦🇩 [5294215205763434181]
8)🇦🇴 [5294516785482062829]
9)🇦🇮 [5292186323342350940]
10)🇦🇬 [5294005972136647964]
11)🇦🇷 [5292208210495689627]
12)🇦🇲 [5291978717508164018]
13)🇦🇼 [5294007002928798927]
14)🇦🇺 [5294444247779399477]
15)🇦🇹 [5291975174160145850]
16)🇦🇿 [5294323533428579078]
17)🇧🇸 [5294031587321600012]
18)🇧🇭 [5294108398516720753]
19)🇧🇩 [5291824687096027834]
20)🇧🇧 [5294526187165471742]
21)🇧🇾 [5294134426018536120]
22)🇧🇪 [5291774466043435275]
23)🇧🇿 [5294171848068584842]
24)🇧🇯 [5293984969746566866]
25)🇧🇹 [5294121983498277263]
26)🇧🇴 [5294201479047957700]
27)🇧🇼 [5294026179957772585]
28)🇧🇷 [5291892229751723900]
29)🇧🇳 [5292098293692650297]
30)🇧🇬 [5294308947719640437]
31)🇧🇫 [5294153164960848949]
32)🇧🇮 [5294051631933967760]
33)🇰🇭 [5294225191562400452]
34)🇨🇲 [5291997306126626950]
35)🇨🇦 [5292290347450259214]
36)🇨🇻 [5292203503211535593]
37)🇨🇫 [5294210571493724819]
38)🇹🇩 [5291780728105753403]
39)🇨🇱 [5294231037012888049]
40)🇨🇳 [5294068833277990704]
41)🇨🇴 [5294010206974397371]
42)🇰🇲 [5294351381996521508]
43)🇨🇬 [5294035229453865597]
44)🇨🇰 [5292098684534675100]
45)🇨🇷 [5292063805105263554]
46)🇨🇮 [5293991322003200135]
47)🇭🇷 [5291999676948569127]
48)🇨🇺 [5291963947115631526]
49)🇨🇾 [5294062721539526918]
50)🇨🇿 [5294242852467923382]
51)🇩🇰 [5294531860817268837]
52)🇩🇯 [5294127214768468283]
53)🇩🇲 [5294485513825178032]
54)🇩🇴 [5294522197140857947]
55)🇪🇨 [5292083733753517221]
56)🇪🇬 [5293992082212409502]
57)🇸🇻 [5294337307388695687]
58)🏴󠁧󠁢󠁥󠁮󠁧󠁿 [5294410107084365278]
59)🇬🇶 [5292170045416297012]
60)🇪🇷 [5291922054004625949]
61)🇪🇪 [5291951143818123103]
62)🇪🇹 [5292245976143124155]
63)🇪🇺 [5291992809295861098]
64)🇬🇮 [5292055799286224027]
65)🇬🇲 [5294399820637688352]
66)🇬🇱 [5292014752283774878]
67)🇫🇮 [5294049961191690629]
68)🇫🇷 [5291817660529533837]
69)🇬🇦 [5294321325815389139]
70)🇬🇪 [5294349389131697267]
71)🇩🇪 [5292013274815028523]
72)🇬🇭 [5294347396266873249]
73)🇬🇷 [5291948395039054764]
74)🇬🇼 [5294409819321550432]
75)🇬🇹 [5294336633078831209]
76)🇬🇳 [5291892096607739008]
77)🇬🇾 [5292062692708736193]
78)🇭🇹 [5292045130587462814]
79)🇭🇳 [5291901034434682297]
80)🇭🇰 [5292166459118606932]
81)🇭🇺 [5294229581018975260]
82)🇮🇸 [5294354358408859664]
83)🇮🇳 [5291933173674957761]
84)🇮🇷 [5294220170745630736]
85)🇮🇶 [5294325010897327367]
86)🇮🇪 [5294471971793293647]
87)🇮🇲 [5294318478252070646]
88)🇮🇱 [5294069056616289553]
89)🇮🇹 [5291826830284709120]
90)🇯🇲 [5294505107465982830]
91)🇯🇵 [5291799063321139445]
92)🇯🇪 [5291950280529697493]
93)🇯🇴 [5291988613112814801]
94)🇰🇿 [5294227175837290463]
95)🇰🇪 [5292111852904416801]
96)🇰🇮 [5294538934628405146]
97)🇰🇵 [5294193812531333564]
98)🇰🇷 [5294408281723262763]
99)🇰🇼 [5292066437920218075]
100)🇰🇬 [5292091954320922577]
101)🇱🇦 [5291981530711746037]
102)🇱🇻 [5292236016113966127]
103)🇱🇧 [5294193108156699621]
104)🇱🇸 [5292040693886247604]
105)🇱🇷 [5291793810576137439]
106)🇱🇾 [5291858711826946840]
107)🇱🇮 [5292048742654957785]
108)🇱🇹 [5294343084119708700]
109)🇱🇺 [5294423709245787718]
110)🇲🇰 [5294023611567332075]
111)🇲🇬 [5291991568050312348]
112)🇲🇼 [5294241881805312589]
113)🇲🇾 [5291858351049696702]
114)🇲🇻 [5292004203844097218]
115)🇲🇱 [5292086972158858331]
116)🇲🇹 [5294532213004588353]
117)🇲🇭 [5294180730060954484]
118)🇲🇷 [5294429743674840973]
119)🇲🇺 [5294127824653797277]
120)🇲🇽 [5294535073452809778]
121)🇫🇲 [5291838156113470124]
122)🇲🇩 [5294158486425325375]
123)🇲🇨 [5294378161117614233]
124)🇲🇳 [5294316532631883496]
125)🇲🇦 [5292108962391414885]
126)🇲🇿 [5294086708931874940]
127)🇲🇲 [5294254478944393569]
128)🇳🇦 [5292021761670404922]
129)🇳🇷 [5294463274484521342]
130)🇳🇵 [5294458756178924088]
131)🇳🇱 [5291917797692042265]
132)🇳🇿 [5294189019347833274]
133)🇳🇮 [5294240825243358100]
134)🇳🇪 [5291809418487290691]
135)🇳🇬 [5294456308047563965]
136)🇳🇺 [5294471336138134209]
137)🇳🇴 [5291761718580502030]
138)🇴🇲 [5291813666209946812]
139)🇵🇰 [5291825606219029010]
140)🇵🇸 [5294289826525238172]
141)🇵🇦 [5291959935616178405]
142)🇵🇬 [5291917995260533077]
143)🇵🇾 [5294525611639852679]
144)🇵🇭 [5291798075478661634]
145)🇵🇪 [5292099427564018941]
146)🇵🇱 [5292190970496963836]
147)🇵🇹 [5294436555492973610]
148)🇵🇷 [5292121516580820347]
149)🇶🇦 [5292166360334357676]
150)🇷🇴 [5294107724206856227]
151)🇷🇺 [5294335323113807278]
152)🇷🇼 [5294191265615729158]
153)🇸🇲 [5292147350809106831]
154)🇸🇹 [5292183188016222701]
155)🇸🇦 [5294163983983463099]
156)🏴󠁧󠁢󠁳󠁣󠁴󠁿 [5294434665707368018]
157)🇸🇳 [5292087023698466689]
158)🇷🇸 [5294458584380230360]
159)🇸🇨 [5291891186074672309]
160)🇸🇱 [5294494314213167952]
161)🇸🇬 [5294451304410663668]
162)🇸🇰 [5294538440707166931]
163)🇸🇮 [5294279359689938006]
164)🇸🇧 [5294283890880433237]
165)🇸🇴 [5294058817414255960]
166)🇿🇦 [5294325281480266304]
167)🇪🇸 [5294513087515216901]
168)🇱🇰 [5292102670264328257]
169)🇸🇩 [5294177148058228060]
170)🇸🇷 [5294396668131692138]
171)🇸🇿 [5294312482477724867]
172)🇸🇪 [5291737091238026321]
173)🇨🇭 [5291791748991835084]
174)🇸🇾 [5294013428199869487]
175)🇹🇼 [5294095745543069603]
176)🇹🇯 [5294120269806328883]
177)🇹🇿 [5292146096678658977]
178)🇹🇭 [5293994384314882755]
179)🇹🇬 [5294097669688415562]
180)🇹🇴 [5294283689016973348]
181)🇹🇹 [5294362935458548705]
182)🇹🇳 [5294484680601521871]
183)🇹🇷 [5293993400767367408]
184)🇹🇲 [5294098958178603764]
185)🇹🇨 [5294320866253884749]
186)🇺🇸 [5294244076533600593]
187)🇺🇬 [5294192317882716626]
188)🇦🇪 [5294314831824835370]
189)🇬🇧 [5293993521026453119]
190)🇺🇦 [5294263837678131580]
191)🇻🇺 [5294448585696368047]
192)🇺🇿 [5294217645304864345]
193)🇺🇾 [5291928449210932974]
194)🇻🇪 [5294476442854247878]
195)🇻🇳 [5294235963340379688]
196)🇻🇮 [5294228039125718124]
197)🏴󠁧󠁢󠁷󠁬󠁳󠁿 [5294139949346476093]
198)🇾🇪 [5294058972033076492]
199)🇿🇲 [5294100109229838880]
200)🇿🇼 [5294422158762592930]"""

# Subdivision / non-ISO tags that the regional-indicator decoder cannot derive.
_SUBDIVISION_FLAGS: Dict[str, str] = {
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿": "England",
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿": "Scotland",
    "🏴󠁧󠁢󠁷󠁬󠁳󠁿": "Wales",
}

# Reverse: canonical name -> the subdivision flag glyph that is this flag's
# ``documentAttributeCustomEmoji.alt`` (Telegram renders a custom emoji entity
# only when the wrapped text character is EXACTLY this emoji).
_SUBDIVISION_ALTS: Dict[str, str] = {v: k for k, v in _SUBDIVISION_FLAGS.items()}

# ISO 3166-1 alpha-2 + regional flags (EU) -> canonical country name used in
# COUNTRY_EMOJI. "EU" is not ISO 3166-1 but 🇪🇺 is the EU flag; it is decoded
# from its e+u regional indicators like everything else.
_ISO2_TO_NAME: Dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AF": "Afghanistan",
    "AG": "Antigua and Barbuda", "AI": "Anguilla", "AL": "Albania",
    "AM": "Armenia", "AO": "Angola", "AR": "Argentina",
    "AS": "American Samoa", "AT": "Austria", "AU": "Australia",
    "AW": "Aruba", "AX": "Åland Islands", "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina", "BB": "Barbados", "BD": "Bangladesh",
    "BE": "Belgium", "BF": "Burkina Faso", "BG": "Bulgaria",
    "BH": "Bahrain", "BI": "Burundi", "BJ": "Benin", "BM": "Bermuda",
    "BN": "Brunei", "BO": "Bolivia", "BR": "Brazil", "BS": "Bahamas",
    "BT": "Bhutan", "BW": "Botswana", "BY": "Belarus", "BZ": "Belize",
    "CA": "Canada", "CC": "Cocos Islands", "CD": "DR Congo",
    "CF": "Central African Republic", "CG": "Congo", "CH": "Switzerland",
    "CI": "Côte d'Ivoire", "CK": "Cook Islands", "CL": "Chile",
    "CM": "Cameroon", "CN": "China", "CO": "Colombia", "CR": "Costa Rica",
    "CU": "Cuba", "CV": "Cape Verde", "CY": "Cyprus", "CZ": "Czechia",
    "DE": "Germany", "DJ": "Djibouti", "DK": "Denmark", "DM": "Dominica",
    "DO": "Dominican Republic", "DZ": "Algeria", "EC": "Ecuador",
    "EE": "Estonia", "EG": "Egypt", "ER": "Eritrea", "ES": "Spain",
    "ET": "Ethiopia", "EU": "European Union", "FI": "Finland", "FJ": "Fiji",
    "FM": "Micronesia", "FR": "France", "GA": "Gabon", "GB": "United Kingdom", "GD": "Grenada",
    "GE": "Georgia", "GH": "Ghana", "GI": "Gibraltar", "GL": "Greenland",
    "GM": "Gambia", "GN": "Guinea", "GQ": "Equatorial Guinea",
    "GR": "Greece", "GT": "Guatemala", "GU": "Guam", "GW": "Guinea-Bissau",
    "GY": "Guyana", "HK": "Hong Kong", "HN": "Honduras", "HR": "Croatia",
    "HT": "Haiti", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IM": "Isle of Man", "IN": "India", "IQ": "Iraq",
    "IR": "Iran", "IS": "Iceland", "IT": "Italy", "JE": "Jersey",
    "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya",
    "KG": "Kyrgyzstan", "KH": "Cambodia", "KI": "Kiribati", "KM": "Comoros",
    "KN": "Saint Kitts and Nevis", "KP": "North Korea", "KR": "South Korea", "KW": "Kuwait",
    "KY": "Cayman Islands", "KZ": "Kazakhstan", "LA": "Laos",
    "LB": "Lebanon", "LC": "Saint Lucia", "LI": "Liechtenstein",
    "LK": "Sri Lanka", "LR": "Liberia", "LS": "Lesotho", "LT": "Lithuania",
    "LU": "Luxembourg", "LV": "Latvia", "LY": "Libya", "MA": "Morocco",
    "MC": "Monaco", "MD": "Moldova", "ME": "Montenegro", "MG": "Madagascar",
    "MH": "Marshall Islands", "MK": "North Macedonia", "ML": "Mali",
    "MM": "Myanmar", "MN": "Mongolia", "MO": "Macao", "MR": "Mauritania",
    "MT": "Malta", "MU": "Mauritius", "MV": "Maldives", "MW": "Malawi",
    "MX": "Mexico", "MY": "Malaysia", "MZ": "Mozambique", "NA": "Namibia",
    "NE": "Niger", "NG": "Nigeria", "NI": "Nicaragua", "NL": "Netherlands",
    "NO": "Norway", "NP": "Nepal", "NR": "Nauru", "NU": "Niue", "NZ": "New Zealand",
    "OM": "Oman", "PA": "Panama", "PE": "Peru", "PG": "Papua New Guinea",
    "PH": "Philippines", "PK": "Pakistan", "PL": "Poland", "PR": "Puerto Rico",
    "PS": "Palestine", "PT": "Portugal", "PW": "Palau", "PY": "Paraguay",
    "QA": "Qatar", "RO": "Romania", "RS": "Serbia", "RU": "Russia",
    "RW": "Rwanda", "SA": "Saudi Arabia", "SB": "Solomon Islands",
    "SC": "Seychelles", "SD": "Sudan", "SE": "Sweden", "SG": "Singapore",
    "SI": "Slovenia", "SK": "Slovakia", "SL": "Sierra Leone",
    "SM": "San Marino", "SN": "Senegal", "SO": "Somalia", "SR": "Suriname",
    "SS": "South Sudan", "ST": "São Tomé and Príncipe", "SV": "El Salvador",
    "SY": "Syria", "SZ": "Eswatini", "TC": "Turks and Caicos Islands",
    "TD": "Chad", "TG": "Togo", "TH": "Thailand", "TJ": "Tajikistan",
    "TL": "Timor-Leste", "TM": "Turkmenistan", "TN": "Tunisia", "TO": "Tonga",
    "TR": "Turkey", "TT": "Trinidad and Tobago", "TW": "Taiwan",
    "TZ": "Tanzania", "UA": "Ukraine", "UG": "Uganda", "US": "United States",
    "UY": "Uruguay", "UZ": "Uzbekistan", "VA": "Holy See", "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela", "VG": "British Virgin Islands", "VI": "US Virgin Islands",
    "VN": "Vietnam", "VU": "Vanuatu", "WS": "Samoa", "YE": "Yemen",
    "ZA": "South Africa", "ZM": "Zambia", "ZW": "Zimbabwe",
}


# Lowercased alias -> canonical name. Kept deliberately conservative so ordinary
# words are never miscounted as countries (e.g. "us" only matches a standalone
# word, never inside "just").
_COUNTRY_ALIASES: Dict[str, str] = {
    "united states of america": "United States",
    "the united states": "United States",
    "usa": "United States",
    "the usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "america": "United States",
    "united kingdom": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "gb": "United Kingdom",
    "england": "United Kingdom",
    "uae": "United Arab Emirates",
    "ksa": "Saudi Arabia",
    "saudi": "Saudi Arabia",
    "eu": "European Union",
    "europe": "European Union",
    "european union": "European Union",
    "czech republic": "Czechia",
    "czechia": "Czechia",
    "the netherlands": "Netherlands",
    "holland": "Netherlands",
    "turkiye": "Turkey",
    "türkiye": "Turkey",
    "korea": "South Korea",
    "south korea": "South Korea",
    "north korea": "North Korea",
    "dprk": "North Korea",
    "ivory coast": "Côte d'Ivoire",
    "cote d'ivoire": "Côte d'Ivoire",
    "cape verde": "Cape Verde",
    "cabo verde": "Cape Verde",
    "the gambia": "Gambia",
    "burma": "Myanmar",
    "sao tome and principe": "São Tomé and Príncipe",
    "macedonia": "North Macedonia",
    "congo-brazzaville": "Congo",
    "republic of the congo": "Congo",
    "the congo": "Congo",
    "swaziland": "Eswatini",
    "south africa": "South Africa",
    "new zealand": "New Zealand",
    "hong kong": "Hong Kong",
    "united states virgin islands": "US Virgin Islands",
    "virgin islands": "US Virgin Islands",
    "scotland": "Scotland",
    "wales": "Wales",
}

# Skip matches inside URLs / @handles so "us"/"uk" in links never count.
_URL_MASK_RE = re.compile(r"(?:https?://|t\.me/|@)\S+", re.IGNORECASE)

# Phrases that partially contain a mapped country term but are actually a
# DIFFERENT (unmapped) territory — blanked before scanning so e.g. "South
# Sudan" never matches Sudan and "DR Congo" never matches Congo.
_COMPOUND_SKIP_RE = re.compile(
    r"("
    r"south[\s_-]+sudan"
    r"|(?:dr|drc)[\s_-]+congo"
    r"|democratic[\s_-]+republic[\s_-]+of[\s_-]+(?:the[\s_-]+)?congo"
    r"|northern[\s_-]+cyprus"
    r")",
    re.IGNORECASE,
)

# Whole-token matcher over every canonical name and alias, longest terms first
# so a longer phrase beats its shorter prefix at the same position.
_TO_CANONICAL: Dict[str, str] = {
    name.lower(): name for name in COUNTRY_EMOJI
}
_TO_CANONICAL.update(_COUNTRY_ALIASES)
_TERMS = sorted(_TO_CANONICAL, key=len, reverse=True)
_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _TERMS) + r")\b",
    re.IGNORECASE,
)

# Strip a leading article from a matched spelling for display ("the USA" -> "USA").
_LEADING_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)

# Connector-only filler that a bare country list may contain between names.
_CONNECTOR_WORDS_RE = re.compile(r"\b(?:and|or|plus|&)\b", re.IGNORECASE)
_CONNECTOR_CHARS_RE = re.compile(r"[\s,;:&|/\\()\[\]+\-–—.…]+")


# ---------------------------------------------------------------------------
# FlagsEmoji2024 dump decoding
# ---------------------------------------------------------------------------
_FLAG_DUMP_LINE_RE = re.compile(
    r"^\s*(?:[0-9]+[\))\s:]*)?(?P<flag>\S+?)\s*\[\s*(?P<doc>\d+)\s*\]\s*$"
)


def iso2_from_flag_emoji(flag: str) -> Optional[str]:
    """Decode a regional-indicator flag glyph to its ISO 3166-1 alpha-2 code.

    🇪🇺 -> "EU", 🇺🇸 -> "US", 🇬🇧 -> "GB". Subdivision tag flags (England,
    Scotland, Wales) and anything non-flag return None — callers must then use
    name_from_flag_emoji() for those.
    """
    letters = []
    for ch in flag or "":
        cp = ord(ch)
        if 0x1F1E6 <= cp <= 0x1F1FF:
            letters.append(chr(cp - 0x1F1E6 + ord("A")))
    return "".join(letters) if letters else None


def name_from_flag_emoji(flag: str) -> Optional[str]:
    """Canonical country name for a flag glyph, or None when unrecognized.

    Subdivision / home-nation flags (🏴󠁧󠁢󠁥󠁮󠁧󠁿 etc.) resolve through
    _SUBDIVISION_FLAGS; two-regional-indicator flags resolve via their ISO code.
    Never guesses: an unknown glyph returns None so the caller can skip it.
    """
    flag = (flag or "").strip().rstrip("\uFE0F")
    if flag in _SUBDIVISION_FLAGS:
        return _SUBDIVISION_FLAGS[flag]
    code = iso2_from_flag_emoji(flag)
    if code:
        return _ISO2_TO_NAME.get(code)
    return None


def build_flags2024_mapping(dump_text: str) -> Tuple[Dict[str, int], List[str]]:
    """Parse a FlagsEmoji2024 "N)FLAG [document_id]" dump into name -> doc id.

    The country name for each row comes ONLY from decoding the flag's Unicode
    code points — never from the row's text (or the user's memory). Rows whose
    glyph does not decode to a known country are skipped and reported in the
    returned error list, so a malformed/unknown row can never attach a wrong
    flag. Returns (mapping, errors).
    """
    mapping: Dict[str, int] = {}
    errors: List[str] = []
    for i, line in enumerate((dump_text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _FLAG_DUMP_LINE_RE.match(line)
        if not m:
            errors.append(f"row {i}: unparsable ({line!r}) — skipped")
            continue
        name = name_from_flag_emoji(m.group("flag"))
        if name is None:
            errors.append(
                f"row {i}: no country decodes from flag {m.group('flag')!r} — skipped"
            )
            continue
        mapping[name] = int(m.group("doc"))
    return mapping, errors


# Reverse of _ISO2_TO_NAME: canonical name -> alpha-2 (for deriving the alt
# emoji anchor when a raw dump glyph is unavailable).
_NAME_TO_ISO2: Dict[str, str] = {v: k for k, v in _ISO2_TO_NAME.items()}


def _flag_glyph_from_iso2(code: str) -> str:
    """Standard flag emoji for an ISO code ('PL' -> 🇵🇱)."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


def _flag_alt_for(name: str) -> Optional[str]:
    """Best-known alt emoji for a country name, or None when undeterminable.

    Subdivision (England/Scotland/Wales) flags keep their exact tag glyph;
    everything else is the standard flag built from its ISO code. This glyph is
    what the custom emoji document's ``alt`` must match for Telegram to render
    the entity — a wrong/None anchor must never be guessed.
    """
    if name in _SUBDIVISION_ALTS:
        return _SUBDIVISION_ALTS[name]
    code = _NAME_TO_ISO2.get(name)
    if code:
        return _flag_glyph_from_iso2(code)
    return None


def build_flags2024_alts(dump_text: str) -> Dict[str, str]:
    """Name -> alt-emoji anchor built from the raw dump's own flag glyphs.

    The dump row's glyph IS the ``documentAttributeCustomEmoji.alt`` for that
    custom emoji, so it is the exact character every flag entity must wrap.
    Returns an empty dict (not a guess) when nothing decodes.
    """
    alts: Dict[str, str] = {}
    for line in (dump_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _FLAG_DUMP_LINE_RE.match(line)
        if not m:
            continue
        name = name_from_flag_emoji(m.group("flag"))
        if name is not None:
            alts[name] = m.group("flag")
    return alts


# Keep the legacy pack active until the FlagsEmoji2024 dump is pasted. The
# module-level conditional must live AFTER build_flags2024_mapping is defined.
if FLAGS2024_DUMP.strip():
    _flags2024 = build_flags2024_mapping(FLAGS2024_DUMP)
    for _err in _flags2024[1]:
        logger.warning("FlagsEmoji2024: %s", _err)
    if _flags2024[0]:
        COUNTRY_EMOJI = _flags2024[0]
        _FLAG_ALTS: Dict[str, str] = build_flags2024_alts(FLAGS2024_DUMP)
else:
    # No dump: derive every alt from the country codes. Subdivision tags are
    # exact; everything else is the standard flag — never a wrong glyph.
    _FLAG_ALTS = {name: alt for name in COUNTRY_EMOJI
                  if (alt := _flag_alt_for(name)) is not None}


# ---------------------------------------------------------------------------
# Country detection / flag attachment
# ---------------------------------------------------------------------------
def _scan_matches(text: str) -> List[dict]:
    """All mapped country mentions in ``text`` with spans, deduped by canonical.

    Each entry: {start, end, spelling, canonical, doc_id}. Spans refer to the
    NFKC-normalized, URL/masked text (same string flag_body_lines splits on).
    """
    masked = _URL_MASK_RE.sub(" ", text or "")
    masked = _COMPOUND_SKIP_RE.sub(" ", masked)
    normalized = unicodedata.normalize("NFKC", masked)
    out: List[dict] = []
    seen = set()
    for m in _KEYWORD_RE.finditer(normalized):
        raw = m.group(0)
        canonical = _TO_CANONICAL[raw.strip().lower()]
        if canonical in COUNTRY_EMOJI and canonical not in seen:
            seen.add(canonical)
            out.append({
                "start": m.start(),
                "end": m.end(),
                "spelling": _LEADING_THE_RE.sub("", raw),
                "canonical": canonical,
                "doc_id": COUNTRY_EMOJI[canonical],
            })
    return out


def detect_countries(text: str) -> List[str]:
    """Return the country names mentioned in ``text``, spelled as written.

    Countries are returned in order of first appearance, deduplicated (a country
    mentioned several times yields exactly one line). The first spelling used is
    kept verbatim, so "USA and US" renders as "USA" while "uk and uk" renders as
    "uk". A leading article is dropped for display only ("the USA" -> "USA").
    Only names present in COUNTRY_EMOJI are returned; anything unmapped is
    silently ignored — nothing is ever fetched or looked up dynamically.
    """
    if not text:
        return []
    return [m["spelling"] for m in _scan_matches(text)]


def canonical_of(name: str) -> str:
    """Canonical country name for any spelling/alias ("USA" -> "United States")."""
    return _TO_CANONICAL[name.strip().lower()]


def flag_body_lines(
    content_lines: List[str],
) -> Tuple[List[Tuple[str, List[Tuple[str, int]]]], Set[str]]:
    """Attach a custom flag to every body line that mentions a mapped country.

    Returns (flagged_lines, covered) where:
      * flagged_lines is [(line, [(alt_emoji, document_id), ...])] — each line
        carries its own flags; duplicate-canonical mentions collapse to one
        flag per line. The ``alt_emoji`` is the EXACT standard emoji that the
        custom document's alt contains (e.g. 🇵🇱 for Poland) — Telegram renders
        the custom emoji only when the entity wraps this exact glyph.
      * covered is the set of CANONICAL countries actually flagged in the body,
        so the caller can skip re-emitting a generated top-level line for them
        (this is exactly what stops the "NO POLAND / POLAND 🇵🇱" duplicate).
    Non-country lines are returned untouched; unmapped countries stay bare (a
    wrong flag is never attached).

    The flag glyphs are appended AFTER sanitization and, being the real alt
    emoji, the sanitizer's emoji-strip can never interfere — they are only ever
    added here, onto already-clean lines, in the final render step.
    """
    flagged: List[Tuple[str, List[Tuple[str, int]]]] = []
    covered: Set[str] = set()
    for raw in content_lines or []:
        ln = (raw or "").strip()
        if not ln:
            continue
        norm = unicodedata.normalize("NFKC", ln)
        matches = _scan_matches(norm)
        if not matches:
            flagged.append((ln, []))
            continue
        # (match, alt_emoji) pairs; a country whose alt cannot be determined is
        # left unflagged rather than risk a guessed anchor.
        anchors = [(m, _FLAG_ALTS.get(m["canonical"])) for m in matches]
        valid = [(m, alt) for m, alt in anchors if alt]
        if not valid:
            flagged.append((ln, []))
            continue
        covered.update(m["canonical"] for m, _ in valid)
        if len(valid) == 1:
            m, alt = valid[0]
            # Exactly two spaces between name and flag — no anchor/placeholder
            # characters are ever visible in the line, just the real alt emoji
            # that the MessageEntityCustomEmoji wraps.
            flagged.append((f"{ln}  {alt}", [(alt, m["doc_id"])]))
            continue
        # Bare national list ("UK, USA, Germany") -> one line per country.
        if not _residual_text(norm, [m for m, _ in valid]).strip():
            for m, alt in valid:
                flagged.append((f"{m['spelling']}  {alt}", [(alt, m["doc_id"])]))
        else:
            # Prose mentioning several countries -> each flag appended in order.
            flagged.append(
                (f"{ln}  " + "".join(alt for _, alt in valid),
                 [(alt, m["doc_id"]) for m, alt in valid])
            )
    return flagged, covered


def _residual_text(norm: str, matches: List[dict]) -> str:
    """Remaining text after removing all matched country spans + connectors.

    Empty => the line was a bare country list (safe to split per name). Non-empty
    => the line carries real prose, so flags are appended in place instead.
    """
    parts = []
    pos = 0
    for m in matches:
        parts.append(norm[pos:m["start"]])
        pos = m["end"]
    parts.append(norm[pos:])
    residual = _CONNECTOR_WORDS_RE.sub(" ", "".join(parts))
    return _CONNECTOR_CHARS_RE.sub("", residual)


def emoji_for(country: str) -> int:
    """Document id for a country, given a canonical name or any alias spelling.

    Resolves any alias ("USA", "ksa", "the UK") to the canonical country and
    returns its document id. Raises KeyError when the term is unmapped.
    """
    canonical = _TO_CANONICAL[country.strip().lower()]
    return COUNTRY_EMOJI[canonical]


def alt_for(country: str) -> str:
    """Exact alt emoji a country's flag entity must wrap (e.g. 🇵🇱 for Poland).

    This is the ``documentAttributeCustomEmoji.alt`` glyph — Telegram renders a
    custom emoji entity ONLY when the text character it wraps is exactly this
    emoji. Raises KeyError when the term is unmapped or the alt is unknown.
    """
    canonical = _TO_CANONICAL[country.strip().lower()]
    try:
        return _FLAG_ALTS[canonical]
    except KeyError:
        raise KeyError(f"no alt emoji for {canonical!r}")


def flag_for(country: str) -> Optional[Tuple[str, int]]:
    """(alt_emoji, document_id) for any alias spelling, or None when unmapped.

    Callers use this when generating a standalone country line: the alt is the
    exact anchor the entity must wrap and the id is the custom emoji document.
    Never raises; an undeterminable flag yields None (the caller skips it).
    """
    try:
        canonical = _TO_CANONICAL[country.strip().lower()]
        return (_FLAG_ALTS[canonical], COUNTRY_EMOJI[canonical])
    except KeyError:
        return None