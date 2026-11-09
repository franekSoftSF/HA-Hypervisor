# HA VMware

Sterowanie laboratorium vCenter z Home Assistanta, gdy HA nie ma do niego bezpośredniego dostępu: statystyki,
zasilanie maszyn, wyłączanie i uruchamianie klastra oraz scenariusze startu. Wersja angielska: [README.md](README.md).

```
Home Assistant + ta integracja  <->  Mosquitto  <--MQTT po TLS--  agent
                                                                    |-- API vCenter
                                                                    |-- API ESXi
                                                                    '-- BMC (Redfish, IPMI), Wake on LAN
```

To agent otwiera połączenie, więc nic nie musi wchodzić do sieci vCenter. Hasła do vSphere i BMC zostają na agencie.

**Agent musi działać poza klastrem**, na zawsze włączonym urządzeniu w sieci zarządzania. Inaczej wyłączy się razem
z klastrem i nic go już nie uruchomi.

Wymaga Home Assistant 2026.11 lub nowszego oraz integracji MQTT.

## Instalacja

### 1. Broker

Mosquitto obok HA, z osobnym kontem agenta ograniczonym do jego tematów:

```
user ha-vmware-agent
topic readwrite ha_vmware/lab/#
```

Przy publicznym adresie HTTPS najprościej użyć MQTT po WebSocket przez to samo proxy (port 443), bez otwierania
nowego portu. Działa też TLS na 8883. Nigdy nie wystawiaj połączenia bez szyfrowania ani dostępu anonimowego.

### 2. Agent

```bash
cd agent
cp config.example.yaml config.yaml
docker compose -f docker-compose.example.yml run --rm --entrypoint ha-vmware-agent ha-vmware-agent genkey
```

Uzupełnij `config.yaml`, a hasła wpisz do `.env`. Potem sprawdź połączenia (niczego nie zmienia) i uruchom:

```bash
docker compose -f docker-compose.example.yml run --rm ha-vmware-agent check --config /config/config.yaml
```

```bash
docker compose -f docker-compose.example.yml up -d
```

Zegar agenta musi być poprawny (NTP), bo polecenie wygasa po dwóch minutach.

Potrzebne konta:

- **vCenter**: rola z operacjami zasilania maszyn, wyłączaniem gościa i `Sessions.ValidateSession`, a dla trybu
  Retreat vCLS także zmianą ustawień zaawansowanych.
- **ESXi**: konto lokalne na każdym hoście, które może sterować maszynami i wyłączyć host. Tryb lockdown musi na to
  pozwalać.
- **BMC**: konto ze sterowaniem zasilaniem. Na Supermicro włącz w ustawieniach BMC IPMI over LAN lub Redfish.

### 3. Home Assistant

1. HACS, Custom repositories, dodaj to repozytorium jako Integration i zainstaluj **HA VMware**.
2. Uruchom ponownie HA.
3. Ustawienia, Urządzenia i usługi, Dodaj integrację, **HA VMware**: podaj `agent_id` i `shared_key`.

## Wyłączanie klastra

1. **Podgląd wyłączenia** buduje plan. Widać go w atrybutach sensora Operacja, nic się nie zmienia.
2. **Odbezpiecz wyłączenie klastra** odblokowuje przycisk na 60 sekund.
3. **Wyłącz klaster** wyłącza maszyny, włącza tryb Retreat vCLS, wyłącza vCenter, a na końcu hosty.

Zanim cokolwiek zostanie zatrzymane, agent sprawdza vCenter, logowanie do ESXi i to, czy każdy host da się ponownie
włączyć. Jeśli coś się nie zgadza albo działa maszyna z listy `protected_vms`, wyłączenie jest odrzucane. Maszyna
vCenter nigdy nie jest wyłączana siłowo.

**Uruchom klaster** włącza hosty, uruchamia vCenter przez ESXi i wychodzi z trybu Retreat.

## Scenariusze

Urządzenia i usługi, HA VMware, **Dodaj scenariusz**. Scenariusz to uporządkowana lista grup: maszyny w grupie
startują razem, a następna grupa na nie czeka (VMware Tools, stan zasilania lub nic, plus dodatkowe opóźnienie).

Uruchomienie scenariusza włącza też potrzebne hosty i vCenter, jeśli są wyłączone. Zatrzymanie działa w odwrotnej
kolejności i nie rusza maszyn używanych przez inny aktywny scenariusz. W automatyzacji:

```yaml
action: ha_vmware.start_scenario
data:
  scenario: Test VDI
```

## Bezpieczeństwo

- Polecenia są podpisane HMAC-SHA256 razem z tematem, wygasają po dwóch minutach, działają tylko raz i nigdy nie są
  retained. Samo konto brokera nie wystarczy do ich wysłania.
- Dane od agenta też są podpisane, sfałszowany stan jest ignorowany.
- Klucz współdzielony jest zapisany we wpisie konfiguracji HA i w konfiguracji agenta.

## Rozwój

```powershell
.\tests\run.ps1 -E2E
```
