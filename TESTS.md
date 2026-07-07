# Plan de test — rig double GoPro (auv006)

But : valider à 100 % le manager, le mode solo, la récupération/EMERGENCY et la cohérence
« caméras attendues ». Tout se pilote depuis `~/dev/swarm-vehicle/gopro_scripts/`.

## Préparation
- 2 GoPro branchées, manager armé : `sudo systemctl start gopro-manager gopro-autorevive`
- 2e terminal ouvert sur les logs : `./manager_log.sh`  (= `journalctl -t gopro -t revive -f`)
- Convention logs : tout est `[LEFT]/[RIGHT] verbe`. Statut : `./gopro_ctl.sh status`.

Coche `[x]` quand le résultat attendu est vu.

---

## A. Duo nominal
- [ ] **A1** `./gopro_ctl.sh status` → `state: READY`, `message: ready 2/2`, `all_ready: true`, `num_cameras: 2`.
- [ ] **A2** Au démarrage (logs) : `[LEFT] found` `[LEFT] armed` `[RIGHT] found` `[RIGHT] armed`.

## B. Enregistrement (duo)
- [ ] **B1** `./gopro_ctl.sh record` → `[LEFT] recording` + `[RIGHT] recording`. `status` → `RECORDING`, `recording 2/2`, `num_recording: 2`.
- [ ] **B2** `./gopro_ctl.sh stop` → `[LEFT] stopped recording` + `[RIGHT] stopped recording`. `status` → `READY`.

## C. Réglages
- [ ] **C1** `./gopro_ctl.sh settings resolution=4K fps=24 fov=Linear` → `[LEFT] settings applied` + `[RIGHT] settings applied` (réponse `success=True`).
- [ ] **C2** Combinaison invalide `./gopro_ctl.sh settings resolution=4K fps=240` → `[LEFT]` + `[RIGHT] settings not applied (4K ... 120fps)`. (Rejeté par la validation **avant** d'être appliqué — rien n'est envoyé à la cam. ⚠️ **ne jamais tester en 5K/5.3K** : brownout « not enough power ».)
- [ ] **C3** `record` puis `settings ...` → `[LEFT]`+`[RIGHT] settings not applied (recording)` ; puis `stop`.

## D. ① Récupération patiente (chute simple en record)
- [ ] **D1** `record` (2 cams), **débranche RIGHT** → `[RIGHT] not filming` (ou `unreachable`) puis `[RIGHT] re-arming` **toutes les ~5 s**.
- [ ] **D2** **Rebranche RIGHT avant ~30 s** → `[RIGHT] recovered`, **pas** d'EMERGENCY. Retour `RECORDING recording 2/2`.
- [ ] **D3** Recommence, **laisse RIGHT débranchée > 30 s** → `[RIGHT] EMERGENCY (...)` à la ~6e tentative. `stop`.

## E. ② Caméra instable (clignote) → EMERGENCY latché
- [ ] **E1** `record`, **débranche/rebranche RIGHT 3 fois** → à la 3e : `[RIGHT] EMERGENCY (unreliable, 3 drops)`.
- [ ] **E2** Même si RIGHT refilme ensuite, l'état **reste `FAULT`** jusqu'au prochain `record`. `stop` puis `record` → repart propre.

## F. Cohérence « attendu » (le fix du jour)  ⚠️ le plus important
- [ ] **F1** À l'arrêt, **débranche RIGHT** → `status` = `INITIALIZING`, `1/2 cameras ready`, `all_ready: false`, `[RIGHT] reviving`. **Jamais `READY`.**
- [ ] **F2** RIGHT toujours débranchée, `record` → `[LEFT] recording` + `[RIGHT] missing`, puis `[RIGHT] EMERGENCY (missing after 6 tries)` (~30 s). `status` = `FAULT` (**pas** « recording 2/2 »).
- [ ] **F3** En record mono, **rebranche RIGHT** → `[RIGHT] found` `armed` `recording`, `[RIGHT] recovered`, retour `recording 2/2`.
- [ ] **F4** `record` (2 cams), **débranche les DEUX** → EMERGENCY **immédiate** : `MISSION COMPROMISED -- no camera filming`. `stop`.

## G. ③ Mode solo
- [ ] **G1** 2 cams, `./gopro_ctl.sh solo RIGHT` → `[LEFT] disabled`, `[RIGHT] solo`, watcher `[LEFT] power off`. LED de LEFT **éteinte**. `status` = `READY`, `ready 1/1`, `num_cameras: 1`.
- [ ] **G2** (anti-flapping) La cam coupée **reste coupée** : aucune boucle `power off`/`power on`. Même si tu **rebranches LEFT physiquement**, elle **reste éteinte** (c'est le but).
- [ ] **G3** En `solo RIGHT`, `record`, **débranche RIGHT** → récup **patiente** (~30 s) puis EMERGENCY (pas immédiate). `stop`.
- [ ] **G4** Bascule `./gopro_ctl.sh solo LEFT` → RIGHT **se rallume** (`[RIGHT] power on`, `found`, `armed`), LEFT se coupe. Pas de double-off ni de blocage.
- [ ] **G5** `./gopro_ctl.sh duo` → la cam coupée se rallume : `[LEFT] enabled`, `[LEFT] power on`, `found`, `armed`. `status` = `ready 2/2`.
- [ ] **G6** `record` puis `solo LEFT` (tente de couper RIGHT qui filme) → `[RIGHT] solo refused (recording)`, RIGHT **pas** coupée. `stop`.
- [ ] **G7** (vol de label) `solo RIGHT`, `sudo systemctl restart gopro-manager.service` → au boot `[LEFT] disabled`, la cam présente reste **RIGHT** (`status num_cameras:1`, `sd_info: RIGHT ...`), `cat .socket_labels` = `2-2:2 LEFT / 2-2:4 RIGHT`. Puis `duo`.

## H. Watcher / revive
- [ ] **H1** À l'arrêt, débranche une cam → au bout de ~6 s : `revive: [RIGHT] power-cycle` (**en label**, pas « socket 2-2:4 »).
- [ ] **H2** Débranche <5 s puis rebranche → la cam revient seule (`found`/`armed`).

## I. Persistance & reboot
- [ ] **I1** `sudo reboot` la Pi → au retour, manager démarre **seul**, les cams s'arment. `journalctl -t gopro -b -1` montre le boot précédent (logs **persistants**).
- [ ] **I2** Un `stop` (ou `sudo systemctl stop gopro-manager`) → `journalctl -u gopro-manager` montre « Deactivated successfully », **pas** « Failed ».
- [ ] **I3** `solo RIGHT`, `sudo reboot` → au retour, LEFT reste **coupée** (solo persistant) ; `duo` la rétablit.

## J. Stabilité
- [ ] **J1** Après une série de tests : `systemctl show -p NRestarts --value gopro-manager.service` → le nombre **n'augmente pas tout seul** (seuls tes restarts explicites comptent).

---

### Notes
- ⚠️ **Puissance** : **jamais de 5K/5.3K** (brownout « not enough power »). Rester en ≤ 4K pour les tests.
- ⚠️ **Vbus / flap** : le flap vient probablement des **power-cycles Vbus**. Le watcher ne coupe que les prises **vides** (jamais une cam allumée), après ~6 s d'absence confirmée. **Ne pas abuser** : évite les débranche/rebranche rapides à répétition (chaque prise vide finit power-cyclée). Si tu veux neutraliser une cam qui clignote, utilise **`solo`** (coupe le Vbus proprement et le garde coupé) plutôt que de la laisser en boucle de revive.
- ⚠️ Tes GoPro ont des **faux-contacts** : elles peuvent flapper d'elles-mêmes (`[X] unplugged`/`found`). Si un test est brouillé par un flap, refais-le une fois les 2 cams stables.
- « attendues » = `labels − solo` : **2 par défaut**, **1 seulement si `solo` explicite**. Une cam qui tombe reste **attendue** (le système râle) — elle ne passe **jamais** en solo toute seule.
