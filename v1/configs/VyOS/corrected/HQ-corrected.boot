interfaces {
    ethernet eth0 {
        address 10.10.0.1/22
    }
    ethernet eth1 {
        address 172.16.7.1/30
    }
    ethernet eth2 {
        address 172.16.7.5/30
    }
    loopback lo {
        address 209.165.202.129/30
    }
}
protocols {
    ospf {
        area 0 {
            network 10.10.0.0/22
            network 172.16.7.0/30
            network 172.16.7.4/30
        }
        default-information {
            originate {
            }
        }
        passive-interface eth0
        passive-interface lo
    }
    static {
        route 0.0.0.0/0 {
            interface lo {
            }
        }
    }
}
system {
    host-name HQ
}
