grep -q "iface eth3" /etc/network/interfaces || cat >> /etc/network/interfaces <<'CFG'

auto eth3
iface eth3 inet static
    address 192.168.122.32/24
CFG
ifup eth3
command -v sshd >/dev/null || apk add openssh
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
rc-update add sshd default
rc-service sshd restart
