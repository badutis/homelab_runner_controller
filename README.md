# rackctl — mašinos pabunda pagal darbą ir užmiega be jo

Kodėl ir kaip — [`docs/valdymas.md`](../../docs/valdymas.md). Čia — kas kur ir kaip įdiegti.

| Kas | Kur | Ką daro |
|---|---|---|
| `rackctl/decide.py` | Orange Pi | kada žadinti ir kada migdyti; gryna logika be tinklo |
| `rackctl/github.py` | Orange Pi | skaito runner'ius ir job'ų eilę (tik GET, ETag) |
| `rackctl/actions.py` | Orange Pi | Wake-on-LAN paketas, užmigdymas per SSH |
| `rackctl/main.py` | Orange Pi | ciklas, `/metrics` MKS Pi, paskutinio prisijungimo laikas diske |
| `orangepi/` | Orange Pi | konfigūracijos pavyzdys ir `systemd` servisas |
| `machine/rack-sleep` | CI mašina | užmigdo, jei nevyksta job'as (išėjimo kodai — `actions.py`) |
| `machine/install.sh` | CI mašina | `rack-sleep`, `sudoers`, ribotas raktas, Wake-on-LAN |

Tik Python standartinė biblioteka (3.9+). Testai:

```bash
cd tools/rack && python3 -m unittest discover -s tests -t .
```

## Diegimas

**CI mašinoje** (kiekvienoje; host1 be slaptažodžio, kitose `sudo` paprašys jo):

```bash
sudo bash tools/rack/machine/install.sh --user host3 --pubkey /tmp/rackctl.pub
```

BIOS'e Wake-on-LAN įjungiamas ranka (`docs/valdymas.md`, 0 žingsnis).

**Orange Pi:**

1. Naudotojas `rackctl` be prisijungimo; kodas į `/opt/rackctl` (katalogas `rackctl/`).
2. `/etc/rackctl/`: `config.json` (iš `orangepi/config.example.json`), `id_ed25519` (raktas
   mašinoms, `0600`, savininkas `rackctl`), `known_hosts` (`ssh-keyscan` kiekvienai
   mašinai), `token-a` ir `token-b` (tik skaitymo raktai, `0600`).
3. `orangepi/rackctl.service` → `/etc/systemd/system/`, `systemctl enable --now rackctl`.
4. Žurnalas: `journalctl -u rackctl -f`. Kol `"dry_run": true`, valdiklis tik rašo, ką
   darytų. Po savaitės tokio stebėjimo — `false`.

## Ko dar nėra

- Pranešimai (`alert`) kol kas tik žurnale; Discord — kai bus nuspręsta, kur.
- host5–8 config'e nėra, nes nežinomi jų MAC ir adresai (`inventorius.md`).
