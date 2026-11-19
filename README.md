# ClipBot – clipuri automate de la streameri (YouTube + Kick)

Bot local care urmărește **IShowSpeed, Kai Cenat, iRaphahell, Adin Ross, Jynxzi și Dylan Page** (YouTube; Kick unde au canal),
găsește singur momentele cele mai interesante (din VOD-uri/video-uri **și din live-uri în timp real**)
și le transformă în clipuri verticale 9:16 (TikTok / Shorts / Reels) de **maximum 40 de secunde**,
cu **transcript**, subtitrări sincronizate pe buze, accent pe cuvintele importante și efecte.

## Pornire

1. Dublu-click pe **`start.bat`** – se deschide o fereastră neagră (botul) și, singură, interfața la <http://127.0.0.1:8765>.
   - Lasă fereastra neagră deschisă (o poți minimiza); **dacă o închizi, botul se oprește**.
   - Dacă botul rulează deja, încă un dublu-click pe `start.bat` doar redeschide interfața.
   - Interfața e o pagină web: o poți închide și redeschide oricând la <http://127.0.0.1:8765> fără să oprești botul.
2. În interfață, fiecare streamer are **secțiunea lui** cu clipurile lui:
   - **⚡ Procesează cele mai noi** – ia cel mai nou video/stream neprocesat de pe fiecare platformă.
   - **🔎 Video recente** – listă cu ultimele video-uri YouTube + VOD-uri Kick; alegi ce să clipuiești.
   - **Câmp link** – lipești orice link YouTube sau Kick VOD (`kick.com/<canal>/videos/<id>`).
   - **Clipping live** – când streamerul intră live, botul înregistrează și scoate clipuri automat.
3. Sus: **Auto video noi** (scanează periodic) și **Auto live (toți)** (urmărește live-urile tuturor).
4. **Activitate** arată progresul fiecărui job, cu log.

Fiecare clip are: player, **titlu** (în video, sus, pe toată durata clipului, și pe site deasupra playerului),
motivele pentru care a fost ales, scor, **transcript complet**, link la sursă (la secunda exactă), descărcare **MP4** și **SRT**.

## 🌐 Arată clipurile altora (galerie publică)

Butonul **🌐 Partajează** (sus în panou) pornește o **galerie separată, doar pentru vizionare și descărcare**
(port 8766). Vizitatorii nu pot șterge, procesa sau schimba nimic; panoul tău (port 8765) rămâne accesibil doar de pe PC-ul tău.

- **În rețeaua locală** – oricine e pe același Wi-Fi/cablu intră pe `http://<IP-ul PC-ului>:8766` (linkul apare în fereastră).
  Dacă Windows întreabă de firewall, alege *Permite* pentru rețele private.
- **Pe internet** – un link public `https://….trycloudflare.com` prin Cloudflare Tunnel (gratuit, fără cont).
  `tools\cloudflared.exe` se descarcă automat prima dată. Linkul **se schimbă la fiecare pornire** a botului.
- Ambele opțiuni rămân pornite și după restart, până le oprești din același buton.

## Cum alege momentele (fără AI extern)

| Semnal | De unde |
|---|---|
| 🔊 Vârfuri de volum în banda vocii (țipete, râsete, hype) + explozii bruște după liniște | audio, comparat cu media mobilă a streamului |
| 💬 Explozii de chat (mesaje/secundă + emote de tip KEKW, 😂, W, ??) | chat live Kick (Pusher) și YouTube; chat replay la VOD-urile YouTube |
| 🔥 „Most replayed” | graficul YouTube (când există) |
| ✂️ Clipuri făcute de comunitate | clipurile Kick ale aceluiași stream |
| 🗣️ Transcript | cuvinte de reacție (EN + RO), exclamații, râsete, ritm de vorbire |

Pentru fiecare vârf candidat, botul transcrie ~1 minut în jur (Whisper `large-v3-turbo` pe GPU) și construiește clipul
ca **setup → reacție**: începe la o propoziție de dinainte și se termină imediat după ce reacția se calmează (≤ 40 s).
Candidații se notează pe tot clipul (hype susținut, densitatea vorbirii, unde cade momentul), iar cei slabi
sunt aruncați – mai bine 2 clipuri foarte bune decât 4 mediocre.

### Sincronizare (lipsync)

- **Audio–video:** la YouTube live, sunetul și imaginea vin pe fluxuri separate; botul păstrează timpii originali
  (`-copyts`), deci vocea rămâne pe buze. Pe VOD-uri se ia mereu pista audio **originală** (nu dublajele automate YouTube).
- **Subtitrări:** după Whisper, fiecare cuvânt e aliniat precis cu un model CTC de forced alignment
  (MMS-300m, ONNX pe GPU) – eroare medie ~15 ms față de ~100 ms la Whisper simplu. Tăieturile clipului
  se fac pe marginile reale ale cuvintelor (nu taie silabe).

### Editare 9:16

| Layout | Când | Cum arată |
|---|---|---|
| **Split** | facecam peste joc (Jynxzi, Speed pe Fortnite, iRaphahell) | bandă de titlu sus, facecam-ul (marginile detectate automat), jocul jos |
| **Crop** | față mare / IRL / podcast (Kai Cenat, Speed IRL) | cadru 9:16 pe toată înălțimea care urmărește fața lin |
| **Blur** | fără față clară | tot cadrul + fundal blurat |

Se alege automat (detectare fețe YuNet), sau îl fixezi la streamer (✎ → „Încadrare 9:16”).

**Accent pe cuvintele importante:** cuvântul spus acum e galben; ~14% din cuvinte (cele de hype, sume/numere,
nume, cuvinte strigate, exclamații, cuvinte rare) sunt **verzi**, iar cele țipate **roșii**. Cuvântul cel mai puternic
apare **singur, mare, cu animație pop**, iar **zoom-ul punch** cade exact pe el. Shake + flash doar la țipete reale.
Plus: grading ușor de culoare + sharpen, bară de progres, audio la -14 LUFS. Fără text cu sursa în video
(linkul la sursă rămâne pe site, la fiecare clip).

### Titluri-hook (AI local)

Titlul de sus îl scrie un model AI care rulează **pe PC-ul tău** (Qwen3-4B-Instruct prin llama.cpp, pe procesor,
ca placa video să rămână liberă pentru Whisper; ~5–13 s pe titlu, fără internet și fără cont). Modelul citește
transcriptul clipului și propune 3 variante după formule de hook (întrebare, „X face ceva neașteptat”, teasing
fără să dea finalul) cu detalii concrete din clip – ex. *WHY DOES ISHOWSPEED GIVE JASON MEDS?*,
*CE A FĂCUT IRAPHAHELL LA FINAL?*. Botul o alege pe cea mai specifică. Dacă modelul lipsește sau dă eroare,
se folosește titlul euristic de dinainte. Se poate opri din `config.json` → `"ai_titles": false`.

### Cenzură

Înjurăturile din subtitrări și titluri se scriu cu steluță (ex. *fu\*k*, *sh\*t*, *bi\*ch*, *pu\*a*, *mu\*e*),
iar insultele rasiale se maschează complet (*n\*\*\*a*). Sunetul rămâne original.

## Unde e totul (tot în folderul acesta)

| Ce | Unde |
|---|---|
| Codul botului | `clipbot\`, interfața în `web\`, pornire `run.py` / `start.bat` |
| Setări + streameri | `config.json` (local, se editează și din interfață: ⚙ Setări, ✎ la streamer); valorile inițiale în `config.default.json` |
| Clipurile finale (.mp4, .srt, .jpg) | `data\clips\<streamer>\` |
| Baza de date cu clipuri | `data\clips.db` |
| Log-uri | `data\logs\clipbot.log` |
| Fișiere temporare / buffer live | `data\work\`, `data\live\` (se curăță singure) |
| Python + pachete | `.venv\` |
| Model Whisper (~1.6 GB) | `models\whisper\` |
| Model sincronizare cuvinte (~630 MB) | `models\aligner\` |
| Detector de fețe YuNet (~230 KB) | `models\face\` |
| Model AI pentru titluri – Qwen3-4B-Instruct Q4 (~2.5 GB, licență Apache-2.0) | `models\llm\` |
| llama.cpp (rulează modelul de titluri, build CPU) | `tools\llama\` |
| deno (necesar lui yt-dlp pentru YouTube) | `tools\deno.exe` |
| cloudflared (link public, descărcat la prima folosire) | `tools\cloudflared.exe` |
| Font subtitrări (Montserrat, licență OFL) | `assets\fonts\` |

**ffmpeg** este cel instalat deja pe sistem (winget `Gyan.FFmpeg`). Dacă vrei și ffmpeg în folder,
pune `ffmpeg.exe` + `ffprobe.exe` în `tools\`.

### Dezinstalare
Șterge folderul `D:\Bot_Clipping`. Nimic altceva nu a fost instalat în afara lui.

### Reinstalare de la zero
`powershell -ExecutionPolicy Bypass -File setup.ps1`, apoi `start.bat`.

### Instalare pe alt PC (din GitHub)

**Ce trebuie instalat înainte** (o singură dată, din PowerShell):

| Program | Comandă | Obligatoriu? |
|---|---|---|
| Windows 10/11 pe 64 de biți | – | da (scripturile și uneltele sunt pentru Windows) |
| Python 3.10 – 3.13 (testat pe 3.13) | `winget install Python.Python.3.13` | da |
| ffmpeg cu libass (subtitrări) | `winget install Gyan.FFmpeg` | da |
| Git (sau descarci ZIP-ul de pe GitHub) | `winget install Git.Git` | doar pentru `git clone` |
| Placă NVIDIA + driver recent (2025+) | de pe nvidia.com | nu – fără ea merge pe procesor, mult mai încet |

După instalare, **redeschide terminalul** (ca PATH-ul să vadă Python și ffmpeg), apoi:

```powershell
git clone https://github.com/andrei10marinescu-git/Bot_Clipping.git
cd Bot_Clipping
powershell -ExecutionPolicy Bypass -File setup.ps1
.\start.bat
```

`setup.ps1` pune totul în folderul proiectului: pachetele Python în `.venv\` (~3 GB, inclusiv librăriile CUDA),
deno, detectorul de fețe, modelul de sincronizare (~630 MB), llama.cpp + modelul AI pentru titluri (~2.5 GB) și fonturile.
La **prima pornire** se descarcă și modelul Whisper (~1.6 GB), deci prima pornire durează câteva minute.
Spațiu necesar: ~10.5 GB + clipurile; recomandat 16 GB RAM.

Un PC nou pornește cu **totul oprit** (fără partajare, fără auto-scanare, fără înregistrare live) – setările vin din
`config.default.json` și se pot porni din interfață. `config.json` (setările tale locale) nu e în repo.

În repo **nu** sunt incluse `.venv\`, `tools\`, `models\` și `data\` (clipuri, bază de date, log-uri).

## Setări utile (⚙ Setări)

- **Sensibilitate live** (implicit 1.0): mai mare = mai multe clipuri din live, mai mică = doar momentele mari.
- **Clipuri / video** și **Clipuri / stream lung**: câte clipuri scoate dintr-un VOD.
- **Durată maximă clip**: maximum 40 s, nu se poate depăși.
- La fiecare streamer (✎): limba (EN/RO, pentru transcriere mai bună), culoarea, **încadrarea 9:16**
  (auto / split / crop / blur) și „decupare laterală” (0–0.3, doar pentru blur). La YouTube poți lipi
  @handle-ul, link-ul canalului sau chiar link-ul unui video – botul găsește singur canalul.

## Probleme frecvente

- **YouTube/Kick nu mai descarcă** → rulează `update.bat` (actualizează yt-dlp).
- **Kick** folosește API-ul neoficial al site-ului; dacă îl schimbă, verificarea live sau chat-ul pot să nu mai meargă temporar.
- Prima pornire durează mai mult, pentru că se încarcă modelul Whisper pe GPU.
- Live-urile se înregistrează doar cât timp botul rulează (fereastra `start.bat` deschisă).
  Dacă PC-ul intră în **sleep**, înregistrarea se oprește; la trezire botul se reconectează singur,
  dar momentele din timpul sleep-ului se pierd. Pentru live-uri lungi, dezactivează sleep-ul.

> Clipurile sunt conținutul streamerilor. Respectă regulile platformelor și ale creatorilor când le postezi
> (credit, programele lor oficiale de clipping etc.).
