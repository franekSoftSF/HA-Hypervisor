# Instalacja

Wersja angielska: [../installation.md](../installation.md).

Kolejność ma znaczenie: najpierw broker, potem agent, a Home Assistant na końcu, bo dodanie integracji sprawdza, czy
agent odpowiada.

```
Home Assistant + integracja  <->  Mosquitto  <--MQTT po TLS--  agent
                                                                 |-- API vCenter (443)
                                                                 |-- API ESXi (443)
                                                                 '-- BMC: Redfish (443), IPMI (623/udp), Wake on LAN
```

## 1. Wymagania

| Co | Szczegóły |
|---|---|
| Home Assistant | 2026.11 lub nowszy, z integracją MQTT. HACS nie jest potrzebny, zastąpił go wbudowany Marketplace |
| Urządzenie dla agenta | Zawsze włączone, **poza klastrem**, w sieci zarządzania vCenter, z Debianem lub Ubuntu (albo Dockerem). Raspberry Pi, mini PC albo VM na innym sprzęcie |
| Sieć od agenta | vCenter i każdy ESXi na 443, każdy BMC na 443 i 623/udp, broker |
| Zegar | NTP na agencie i na Home Assistancie. Polecenie wygasa po dwóch minutach |
| Hosty | Nazwy dokładnie takie jak w vCenter |

Nie instaluj agenta w klastrze ani na appliance vCenter. Wyłączyłby się razem z klastrem i nic nie mogłoby go ponownie
uruchomić.

## 2. Broker

Home Assistant i agent muszą korzystać z tego samego brokera. Poniższe kroki dotyczą aplikacji Mosquitto w Home
Assistancie. Na własnym Mosquitto ustaw takie samo konto i ACL.

### Konto

W konfiguracji aplikacji Mosquitto dodaj login dla agenta:

```yaml
logins:
  - username: ha-hypervisor-agent
    password: uzyj-dlugiego-losowego-hasla
```

Włącz ACL, aby to konto miało dostęp tylko do swoich tematów:

```yaml
customize:
  active: true
  folder: mosquitto
```

`/share/mosquitto/acl.conf`:

```
acl_file /share/mosquitto/accesscontrollist
```

`/share/mosquitto/accesscontrollist`, z `lab` zamienionym na Twój `agent_id`. Home Assistant potrzebuje pełnego
dostępu:

```
user addons
topic readwrite #

user homeassistant
topic readwrite #

user ha-hypervisor-agent
topic readwrite ha_hypervisor/lab/#
```

Uruchom ponownie aplikację Mosquitto.

### Jak agent łączy się z brokerem

**Opcja A: MQTT po WebSocket przez reverse proxy (port 443).** Bez otwierania nowego portu. Aplikacja Mosquitto
nasłuchuje WebSocket na 1884. Przykład dla nginx:

```nginx
server {
    listen 443 ssl;
    server_name mqtt.example.com;
    # ssl_certificate i ssl_certificate_key jak dla innych serwisów

    location /mqtt {
        proxy_pass http://IP_HOME_ASSISTANTA:1884;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

W Nginx Proxy Manager dodaj proxy host na port 1884 i włącz **Websockets Support**.

**Opcja B: MQTT po TLS na porcie 8883.** Podaj aplikacji Mosquitto certyfikat (`certfile`, `keyfile`) i przekieruj na
nią port 8883.

Nigdy nie wystawiaj portów 1883 ani 1884 bezpośrednio do internetu.

## 3. Konta vSphere

### vCenter

Utwórz rolę i przypisz ją kontu agenta na poziomie głównym vCenter, z propagacją:

| Uprawnienie | Do czego |
|---|---|
| Virtual machine, Interaction, Power on | Uruchamianie maszyn |
| Virtual machine, Interaction, Power off | Wyłączanie maszyn, także przez system gościa |
| Global, Settings | Tryb Retreat vCLS |

### ESXi

Na każdym hoście utwórz konto lokalne (np. `svc-ha`) i nadaj mu rolę Administrator na tym hoście. Agent używa go do
wyłączania i uruchamiania maszyny vCenter oraz wyłączania hosta, czyli wtedy, gdy vCenter nie działa.

Przy włączonym trybie lockdown dodaj konto do **Exception Users**.

## 4. Sterowanie zasilaniem serwera

Uruchomienie klastra wymaga sposobu na włączenie całkowicie wyłączonego hosta.

**BMC Supermicro:**

1. W panelu BMC włącz **IPMI over LAN** i Redfish (Configuration, Port).
2. Utwórz konto ze sterowaniem zasilaniem (Operator lub wyższe).
3. Nadaj BMC stały adres widoczny dla agenta i nie wystawiaj go do sieci publicznych.

Agent próbuje najpierw Redfish, potem IPMI. Na niektórych płytach i wersjach firmware część funkcji Redfish wymaga
licencji i wtedy agent przechodzi na IPMI.

**Wake on LAN** to opcjonalna ostateczność. Karta sieciowa i BIOS muszą obsługiwać wybudzanie z pełnego wyłączenia,
agent musi być w tym samym segmencie sieci, a działania nie da się sprawdzić z wyprzedzeniem. Przetestuj je raz ręcznie.

## 5. Agent

### Instalacja paczki

Paczki dla Debiana 12 lub nowszego i Ubuntu 22.04 lub nowszego, na amd64 i arm64 (także Raspberry Pi). Paczka ma własnego
Pythona i nie rusza systemowego.

```bash
VERSION=0.3.0
ARCH=$(dpkg --print-architecture)
wget "https://github.com/franekSoftSF/HA-Hypervisor/releases/download/$VERSION/ha-hypervisor-agent_${VERSION}_$ARCH.deb"
sudo apt install "./ha-hypervisor-agent_${VERSION}_$ARCH.deb"
```

Usługa jest włączona, ale czeka na konfigurację.

### Konfiguracja połączeń

Utwórz konfigurację. Polecenie wypisze **ID agenta i klucz współdzielony**, potrzebne później w Home Assistancie:

```bash
sudo ha-hypervisor-agent init --agent-id lab
```

MQTT, dla opcji A z sekcji 2. Dla opcji B użyj `--transport tcp --port 8883`:

```bash
sudo ha-hypervisor-agent mqtt set --host mqtt.example.com --transport websockets --username ha-hypervisor-agent
```

```bash
sudo ha-hypervisor-agent mqtt test
```

vCenter. `--vm-name` to nazwa maszyny vCenter, jeśli działa na zarządzanych hostach:

```bash
sudo ha-hypervisor-agent vcenter set --host vcenter.lab.local --username svc-ha@vsphere.local --vm-name "VMware vCenter Server"
```

```bash
sudo ha-hypervisor-agent vcenter test
```

Test pokazuje też hosty znane vCenter, więc widać dokładne nazwy do użycia poniżej. Dodaj każdy host ESXi razem z jego
BMC:

```bash
sudo ha-hypervisor-agent esxi add esxi01.lab.local --username svc-ha --bmc-address 10.0.0.21 --bmc-username ADMIN
```

```bash
sudo ha-hypervisor-agent esxi test
```

O hasła polecenia pytają interaktywnie, nigdy nie podaje się ich jako argumentów. Potem sprawdź całość i uruchom:

```bash
sudo ha-hypervisor-agent check
```

```bash
sudo systemctl start ha-hypervisor-agent
```

```bash
journalctl -u ha-hypervisor-agent -f
```

Każdy wiersz `check` powinien zaczynać się od `OK`. `WARN` oznacza, że BMC nie odpowiedział i został tylko Wake on LAN,
którego nie da się sprawdzić. W logu powinien pojawić się wpis `Connected to the broker`.

### Lista poleceń

| Polecenie | Do czego |
|---|---|
| `init --agent-id NAZWA` | Tworzy konfigurację i klucz współdzielony |
| `show` | Pokazuje konfigurację bez sekretów i czego jeszcze brakuje |
| `check` | Sprawdza wszystkie połączenia, niczego nie zmienia |
| `key show`, `key rotate` | Pokazuje lub wymienia klucz współdzielony |
| `mqtt set`, `mqtt test` | Połączenie z brokerem |
| `vcenter set`, `vcenter test` | Połączenie z vCenter |
| `esxi add NAZWA`, `esxi add NAZWA --update` | Dodaje lub zmienia host |
| `esxi list`, `esxi remove NAZWA`, `esxi test [NAZWA]` | Lista, usuwanie i test hostów |

Każde polecenie ma `--help`. Opcje `esxi add` to m.in. `--address`, `--bmc-type`, `--wol-mac`, `--wol-broadcast`,
`--no-bmc` i `--no-wol`.

Po zmianie konfiguracji uruchom usługę ponownie: `sudo systemctl restart ha-hypervisor-agent`.

W skryptach `--password-stdin` czyta hasło ze standardowego wejścia (`esxi add` czyta najpierw hasło ESXi, a potem hasło
BMC z `--bmc-password-stdin`). `--password-env NAZWA` zapisuje tylko odwołanie, a wartość trzymasz w
`/etc/ha-hypervisor-agent/agent.env`, który wczytuje usługa.

Konfiguracja jest w `/etc/ha-hypervisor-agent/config.yaml`, dostępna tylko dla roota i agenta.
`apt purge ha-hypervisor-agent` usuwa ją razem z hasłami.

### Alternatywa: Docker

```bash
git clone https://github.com/franekSoftSF/HA-Hypervisor.git
cd HA-Hypervisor/agent
cp config.example.yaml config.yaml
cp .env.example .env
chmod 600 .env config.yaml
docker compose -f docker-compose.example.yml run --rm ha-hypervisor-agent genkey
```

Klucz i hasła wpisz do `.env`, uzupełnij `config.yaml`, a potem:

```bash
docker compose -f docker-compose.example.yml run --rm ha-hypervisor-agent check --config /config/config.yaml
```

```bash
docker compose -f docker-compose.example.yml up -d
```

## 6. Home Assistant

1. Skonfiguruj integrację **MQTT** z tym samym brokerem, jeśli jej jeszcze nie ma.
2. Przejdź do **Ustawienia, Marketplace** i zaakceptuj ostrzeżenie. Wybierz **Add from link** i połącz konto GitHub, gdy
   HA o to poprosi: Marketplace wymaga tego przy repozytoriach dodawanych linkiem.
3. Wpisz `https://github.com/franekSoftSF/HA-Hypervisor`, wybierz **Integration**, jeśli pojawi się pytanie o typ, i
   wybierz **Add**. Otwórz **HA Hypervisor** i zainstaluj. Uruchom HA ponownie tylko wtedy, gdy poprosi o to naprawa.
4. **Ustawienia, Urządzenia i usługi, Dodaj integrację, HA Hypervisor.** Podaj `agent_id` i klucz współdzielony.

Hosty, maszyny i datastore'y pojawią się jako urządzenia w ciągu jednego cyklu odpytywania.

![Strona integracji](../images/pl/integration.png)

### Scenariusze

Na stronie integracji wybierz **Dodaj scenariusz**, podaj nazwę i kolejno dodawaj grupy maszyn.

![Kreator scenariusza](../images/pl/scenario-wizard.png)

### Panel

Utwórz panel, otwórz edytor surowej konfiguracji i wklej [dashboard.example.yaml](../dashboard.example.yaml), zamieniając
`lab`, nazwy maszyn i nazwy scenariuszy na swoje.

![Przykładowy panel](../images/pl/dashboard.png)

## 7. Pierwszy test

Idź krok po kroku, najlepiej gdy masz fizyczny dostęp do serwera:

1. Wyłącz i włącz przełącznikiem jedną nieważną maszynę.
2. Naciśnij **Podgląd wyłączenia** i odczytaj atrybut `plan` sensora **Operacja**. Sprawdź listę maszyn, host z vCenter
   oraz ewentualne ostrzeżenie `wake_unverified`.
3. Włącz raz host z panelu BMC, aby potwierdzić, że BMC działa przy wyłączonym hoście.
4. Wykonaj pełny cykl: **Odbezpiecz wyłączenie klastra**, **Wyłącz klaster**, poczekaj, a potem **Uruchom klaster**.

## Rozwiązywanie problemów

| Objaw | Co sprawdzić |
|---|---|
| Agent nie został znaleziony przy dodawaniu integracji | Agent działa, ma w logu `Connected to the broker`, a HA używa tego samego brokera |
| Inny klucz przy dodawaniu integracji | Ten sam `shared_key` w `.env` i w Home Assistancie |
| `is not a valid app repository` | To sklep aplikacji. Integracje dodaje się w **Ustawienia, Marketplace, Add from link** |
| Akcje kończą się  | Zegary po obu stronach (NTP), wpis `Rejected command` w logu agenta |
| `Not authorised` w logu agenta | Konto i ACL brokera, `agent_id` w ACL |
| `no_wake_method` w podglądzie | BMC nie odpowiada: uruchom `check` |
| `esxi_unavailable` w podglądzie | Konto ESXi, tryb lockdown, port 443 od agenta |
| `host_not_configured` | `hosts[].name` różni się od nazwy w vCenter |
| Klaster zostaje `degraded` po starcie | Usługi vCenter potrzebują kilku minut, obserwuj krok operacji |

## Aktualizacja

Integracja: przez Marketplace. Agent: zainstaluj nową paczkę przez `apt install`, konfiguracja zostaje. W Dockerze:
`git pull` i `docker compose -f docker-compose.example.yml up -d --build`.
