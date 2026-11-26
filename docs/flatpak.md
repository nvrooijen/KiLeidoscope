# Blender from Flathub

Use this guide if your Blender is the Flathub Flatpak (`org.blender.Blender`), for example on Fedora. Flatpak runs Blender in a sandbox, so KiLeidoscope needs three one-time steps.

Tested on Ubuntu 26.04 LTS with KiCad 10.0.6, Flatpak 1.16.6, Blender 5.3.0 from Flathub and KiLeidoscope 0.5.4.

You need:

- KiCad 10 from your distribution or KiCad's packages, not the KiCad Flatpak.
- Blender 5.1 or newer from Flathub: `flatpak info org.blender.Blender` shows Origin `flathub`. To install it, run `flatpak install flathub org.blender.Blender`. On Fedora, turn on third-party repositories first.

## 1. Tell KiCad where Blender is

In KiCad, open **Preferences → Configure Paths** and add:

| Name | Path |
|---|---|
| `KILEIDO_BLENDER` | `/var/lib/flatpak/exports/bin/org.blender.Blender` |

If you installed Blender with `--user`, use `/home/<you>/.local/share/flatpak/exports/bin/org.blender.Blender`. Write the full path; `~` doesn't work here.

## 2. Let Blender read the board

KiLeidoscope keeps a live copy of your board in `/tmp`, which the sandbox can't see by default:

```
flatpak override --user --filesystem=/tmp org.blender.Blender
```

## 3. Let Blender use KiCad's kicad-cli

Silkscreen, solder mask and 3D models come from `kicad-cli`, which is outside the sandbox. Create a small helper and allow Blender to run it. This lets Blender run any command outside its sandbox.

```
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec flatpak-spawn --host --watch-bus --directory="$HOME" kicad-cli "$@"\n' > ~/.local/bin/kicad-cli-host
chmod +x ~/.local/bin/kicad-cli-host
flatpak override --user --talk-name=org.freedesktop.Flatpak --env=KILEIDO_KICAD_CLI=$HOME/.local/bin/kicad-cli-host org.blender.Blender
```

## Open your board

Close any open Blender window, then click **Open in Blender** in the PCB Editor. The first time, the 3D models can take up to a minute to appear.

Each board opens in its own Blender window; "Add here" isn't offered.

## Troubleshooting

Blender's output is in `~/.cache/kileidoscope/blender.log`.

| You see | Fix |
|---|---|
| Stuck on "Finding 3D models…" | Do step 2, then close Blender and click again. |
| "kicad-cli was not found" | Do step 3. |
| "KILEIDO_BLENDER does not point to a Blender executable" | Check the path from step 1. |

## Undo

```
flatpak override --user --reset org.blender.Blender
rm ~/.local/bin/kicad-cli-host
```

The reset also clears any other Blender overrides you made, for example in Flatseal. Then remove `KILEIDO_BLENDER` from **Configure Paths** and restart KiCad.
