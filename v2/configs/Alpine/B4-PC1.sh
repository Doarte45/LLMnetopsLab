cat > /etc/network/interfaces <<'CFG'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address 10.2.1.10/24
    gateway 10.2.1.1
CFG
rc-service networking restart
setup-hostname B4-PC1
rc-service hostname restart
