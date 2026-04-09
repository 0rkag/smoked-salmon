#!/bin/bash
# Corpus recapture script. NOT set -e — we want to see per-entry failures.

uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/[075679890696] VA - I Want You vs. Operator Remixes' --beatport https://www.beatport.com/release/i-want-you-vs-operator-remixes/6474379 --discogs https://www.discogs.com/release/11368204-Chris-Lake-I-Want-You-vs-Operator-Remixes --apple-music https://music.apple.com/us/album/i-want-you-vs-operator-remixes-ep/1257932414
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[EGLO002] Floating Points - Vacuum EP' --bandcamp https://floatingpoints.bandcamp.com/album/vacuum-boogie-ep --discogs https://www.discogs.com/master/243046-Floatingpoints-Vacuum-EP --musicbrainz https://musicbrainz.org/release/8906ed74-8192-43fd-a447-615c329d5f62
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[ETUI LTD.004] Insect O. - Bondi Dub' --discogs https://www.discogs.com/release/4612731-Insect-O-Bondi-Dub
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[FORUM IV] Sa Pa - 風物詩/' --discogs https://www.discogs.com/release/7268171-Sa-Pa-%E9%A2%A8%E7%89%A9%E8%A9%A9
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[HOSTOM004] Unknown Artist - HOSTOM - 004' --discogs https://www.discogs.com/master/1481355-Hostom-HOSTOM-004 --musicbrainz https://musicbrainz.org/release/0bfa1f6f-ad6c-40bc-beef-27086a0c31b6/details
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[HOSTOM005] Unknown Artist - HOSTOM - 005' --discogs https://www.discogs.com/release/10501765-Hostom-HOSTOM-005 --musicbrainz https://musicbrainz.org/release/140433ea-321d-4f81-81c7-e01bae99d9e7
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[HOSTOMZZZ] Unknown Artist - HOSTOM - ZZZ' --discogs https://www.discogs.com/release/8572819-Hostom-HOSTOM-ZZZ --musicbrainz https://musicbrainz.org/release/f2c80323-bae5-4d6b-a70c-ca87165a11ae
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MOI001] MOi - 01/' --discogs https://www.discogs.com/release/6983004-MOi-01
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MOI003] MOi - 03/' --discogs https://www.discogs.com/master/1088290-MOi-03
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MOI006] MOi - 06/' --discogs https://www.discogs.com/master/4146157-Moi-06
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MOI008] MOi - 08/' --discogs https://www.discogs.com/master/4141756-Moi-08

# --- Entries below are MISSING provider URLs; they will FAIL until URLs are added. ---
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULEN008] iO - The Barefooter Remixes/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULEN009] iO - The Barefooter Remixes #2/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULEN010] Silat Beksi - 8th Reality/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULENV006] Varhat - Unknown Cut/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULENV012] Fabe - Break For EP/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULENV014] Premiesku - Other EP/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[MULENV015] iO - Chemical EP/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[RAWAX-S07] Enzo Siragusa & Seb Zito - Woonie Trax EP/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[SCL005] Unknown Artist - Social 5/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[SCL006] Unknown Artist - Social 6/'
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/[SCL008] Unknown Artist - Social 8/'
# --- End of entries missing URLs ---

uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/#savefabric - Compilation' --bandcamp https://savefabric.bandcamp.com/album/compilation --musicbrainz https://musicbrainz.org/release/fb6cf2e0-b40d-4840-bbd9-709f59b344e7 --discogs https://www.discogs.com/release/9279323-Various-savefabric
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/BABON - Tropical Desert/' --discogs https://www.discogs.com/master/3998215-Babon-Tropical-Desert --apple-music https://music.apple.com/us/album/tropical-desert/1813955620 --bandcamp https://babon.bandcamp.com/album/tropical-desert --musicbrainz https://musicbrainz.org/release/d4ea50c1-63e4-4590-9804-2850f751bfed/cover-art
uv run python benchmarks/capture.py --force --source vinyl 'benchmarks/corpus/files/Border One - Light Trail EP/' --bandcamp https://borderonerecords.bandcamp.com/album/light-trail-ep --discogs https://www.discogs.com/release/27435897-Border-One-Light-Trail-EP --apple-music https://music.apple.com/in/album/light-trail-ep/1687287309
uv run python benchmarks/capture.py --force 'benchmarks/corpus/files/Burial - Subtemple' --discogs https://www.discogs.com/master/1182270-Burial-Subtemple --bandcamp https://burial.bandcamp.com/album/subtemple --tidal https://tidal.com/album/111933119 --apple-music https://music.apple.com/mt/album/subtemple-beachfires-ep/1238656816
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Cravagoide - Hidden Sanctuary/' --bandcamp https://pulse-state.bandcamp.com/album/hidden-sanctuary --discogs https://www.discogs.com/release/35361634-Cravagoide-Hidden-Sanctuary --musicbrainz https://musicbrainz.org/release/0c13a9a1-5642-4efc-8693-db0c575a1f70
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/DJ SWISHERMAN - DRINK N SMOKE/' --bandcamp https://djswisherman.bandcamp.com/track/drink-n-smoke --apple-music https://music.apple.com/us/album/drink-n-smoke-single/1674037996
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/DJ SWISHERMAN - SMOKED OUT/' --bandcamp https://djswisherman.bandcamp.com/track/smoked-out --apple-music https://music.apple.com/us/album/smoked-out-single/1679241798
uv run python benchmarks/capture.py --force --source web "benchmarks/corpus/files/DJ SWISHERMAN - THAT'S THE SIZZURP/" --bandcamp https://djswisherman.bandcamp.com/album/thats-the-sizzurp --apple-music https://music.apple.com/us/album/thats-the-sizzurp-single/1694462325
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Hiroaki Iizuka - Recall/' --bandcamp https://artsrecordings.bandcamp.com/album/recall --discogs https://www.discogs.com/master/3045458-Hiroaki-Iizuka-Recall --apple-music https://music.apple.com/us/album/recall-ep/1660558223

uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 1' --bandcamp https://kcik.bandcamp.com/track/kcik-1
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 3' --bandcamp https://kcik.bandcamp.com/track/kcik-3 --discogs https://www.discogs.com/release/33700986-Kcik-Kcik-3
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 8' --bandcamp https://kcik.bandcamp.com/track/kcik-8 --discogs https://www.discogs.com/release/33701109-Kcik-Kcik-8
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 11' --discogs https://www.discogs.com/release/33702015-Kcik-Kcik-11 --bandcamp https://kcik.bandcamp.com/track/kcik-11
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 14' --bandcamp https://kcik.bandcamp.com/track/kcik-14
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 19' --bandcamp https://kcik.bandcamp.com/track/kcik-19
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 20' --bandcamp https://kcik.bandcamp.com/track/kcik-20
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 22' --bandcamp https://kcik.bandcamp.com/track/kcik-22
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 23' --bandcamp https://kcik.bandcamp.com/track/kcik-23
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 24' --bandcamp https://kcik.bandcamp.com/album/kcik-24
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 27' --bandcamp https://kcik.bandcamp.com/album/kcik-27
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Unknown Artist - Kcik 31' --bandcamp https://kcik.bandcamp.com/track/kcik-31

uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Various Artists - Chronic Girl/' --bandcamp https://enemyrecords.bandcamp.com/album/chronic-girl --discogs https://www.discogs.com/release/5034974-Various-Chronic-Girl --beatport https://www.beatport.com/release/chronic-girl/865623
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Wangan Club - MOST WANTED 06 -WC015' --bandcamp https://wanganclub.bandcamp.com/album/most-wanted-06-wc015
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Wangan Club - wemory - The Process/' --bandcamp https://wanganclub.bandcamp.com/album/wemory-the-process --apple-music https://music.apple.com/us/album/the-process/1781096448
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Wangan Club - WC009- Wangan Club 2 Años Aniversario V.A' --bandcamp https://wanganclub.bandcamp.com/album/wc009-wangan-club-2-a-os-aniversario-v-a --musicbrainz https://musicbrainz.org/release/d58f83f2-f02f-4cfc-92a9-98ba514497c4
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/Wangan Club - WC016- Wangan Club 3 Años Aniversario V.A' --musicbrainz https://musicbrainz.org/release/8d7852c2-2ac5-451b-a19b-f4c8ce07e7f8 --bandcamp https://wanganclub.bandcamp.com/album/wc016-wangan-club-3-a-os-aniversario-v-a
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/ZeroFG - 3FD Collection' --bandcamp https://zerofg.bandcamp.com/album/3fd-collection
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/ᴄʟᴇʀɢʏ - Albert Zhirnov - Switchback EP -CRG028/' --bandcamp https://bill-van-lookc.bandcamp.com/album/switchback-ep --discogs https://www.discogs.com/release/25870618-Albert-Zhirnov-Switchback-EP --beatport https://www.beatport.com/release/switchback-ep/3979739

uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/アンと私 - NARCISM (2026) [WEB 24bit FLAC]/' --apple-music https://music.apple.com/jp/album/narcism/1866479541
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/坂本慎太郎 - ヤッホー/' --apple-music https://music.apple.com/jp/album/yoo-hoo/1854043241
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/窦唯 & 朝简 - 景福殿赋 (2026) [WEB FLAC]/'
uv run python benchmarks/capture.py --force --source web 'benchmarks/corpus/files/那由他計画 - Project NAYUTA - 2026 - Uniomisty/' --bandcamp https://nayutakeikaku.bandcamp.com/album/uniomisty
