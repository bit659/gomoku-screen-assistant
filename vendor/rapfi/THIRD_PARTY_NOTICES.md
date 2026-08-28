# Rapfi third-party notice

This directory contains the Rapfi Gomoku/Renju engine release `250615` from
<https://github.com/dhbloo/rapfi>.

- Upstream tag: `250615`
- Upstream commit: `1be1551ced57e38d53ed58f6d74bf6f8b4bdc230`
- Official release asset: <https://github.com/dhbloo/rapfi/releases/download/250615/Rapfi-engine.7z>
- Release asset SHA-256: `1A3E24024062A153AC079060EE9589A37C6BDD1ECC54FED3908793C519594E05`
- Engine license: GNU General Public License version 3, in `COPYING.txt`
- Corresponding source: `rapfi-source-250615.zip`
- Corresponding source SHA-256: `A2A410DE094F5813808CB99AC278C0180A0F26694E5F015A87FAE082B22D2EFC`
- Neural-network weights: CC0 1.0. The complete terms are in
  `NETWORK_WEIGHTS_CC0.txt`; the upstream source is
  <https://github.com/dhbloo/rapfi-networks>.

The local `config.toml` limits the release to its freestyle NNUE weight and
disables the optional position database. The engine executable and model files
are otherwise unmodified from the official `Rapfi-engine.7z` release asset.

Bundled upstream file SHA-256 values:

- `pbrain-rapfi-windows-avx2.exe`: `1E02497433FB765626F954C895B0B92E057AC12A76C76318ABE74D4050914E8E`
- `pbrain-rapfi-windows-sse.exe`: `DC98082D74E862547FD7F45130399782F21F6023AE15676A2AD471ECD1DACA4C`
- `mix9svqfreestyle_bsmix.bin.lz4`: `6BC0D1B0FF8E1D857F7F412923CD458F38F1A435087C91AE71FEC3676C23EF62`
- `model210901.bin`: `2FA08CB15CF240D9533548C3E011B03D46809F6858362E97EE873D0632AA2015`
