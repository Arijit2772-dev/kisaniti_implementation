# Phase 3 step 0 — server prepared (CHAT-54), 23 Nov 2026

**Scope.** One-time preparation of `kisaniti-vm` (Azure for Students, India South Central, Standard_B1s, Ubuntu 24.04.5, static IP 172.128.33.73), by `deploy/server_setup.sh`. No application deployed yet; HTTPS waits for the DNS records of `kisaniti.in` (domain still "Pending" at the registrar on 23 Nov).

**What it does.** System updates and daily unattended security updates; 2 GB swap with swappiness 10 (887 MB RAM); Docker 29.1.3 and Compose 2.40.3 from Ubuntu's archive; SSH keys only and no root login.

**Perimeter.** Azure network security group `kisaniti-vmNSG`: inbound 22, 80, 443, 8883 only. ufw is deliberately not used: Docker-published ports bypass it, so the production compose file (next step) will publish only Caddy (80/443) and the broker (8883); PostgreSQL, the API and the website stay on the internal network.

**Checks (run 23 Nov 2026).** `sshd -T`: passwordauthentication no, kbdinteractiveauthentication no, permitrootlogin no; a password login attempt is refused (`Permission denied (publickey)`); only port 22 listens; swap 2047 MB active; the script ran a second time without change (idempotent).

**Not done / not verified.** Actual Azure cost in this region (price not in the public list; first cost data in a few days); memory use of the full stack on this VM.
