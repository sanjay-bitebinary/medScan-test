# Office GPU bridge

The office server must initiate a reverse SSH tunnel to the public application host. The tunnel exposes no GPU service publicly:

`public host 127.0.0.1:18003 -> SSH reverse tunnel -> GPU 127.0.0.1:8003`

The public Docker app reaches the host-side tunnel through `host.docker.internal:18003`.

The GPU server already has dots.ocr cached and running on `CUDA_VISIBLE_DEVICES=1`. GPU 0 remains reserved for the 31B model.

Before enabling the tunnel, add `~/.ssh/medscan_public_tunnel_ed25519.pub` to the public host's `/root/.ssh/authorized_keys`, restricted to the tunnel if possible.
