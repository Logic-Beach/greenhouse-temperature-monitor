# Greenhouse temperature monitor

A small Raspberry Pi program that watches greenhouse temperature, flips heater/fan relays, and serves a simple status page. Outdoor weather from Open-Meteo is graphed next to the greenhouse probe for comparison. 

## Hardware

Built around a **Raspberry Pi Zero W** running **DietPi**.

| Piece | Role |
| --- | --- |
| Pi Zero W | Runs DietPi, `tempwatch.py`, and the local web server |
| Temperature probe on **GPIO 4** (physical pin 7) | DS18B20 (1-Wire) or DHT11/DHT22 |
| Relay on **GPIO 17** (physical pin 11) | Cold / heater side |
| Relay on **GPIO 27** (physical pin 13) | Hot / fan side |

Wiring notes:

- Power the probe from **3.3V** and ground. 
- A bare DS18B20 needs a **4.7kΩ** pull-up resistor between data and 3.3V. (Probe I used: https://www.amazon.com/dp/B0FLDQJ71M?th=1, has resistor in the wiring)
- The Pi only drives the relay **inputs**. Heater and fan power are on the relay contacts. (Relay I used: https://www.amazon.com/dp/B0GR85JF68?ref=ppx_yo2ov_dt_b_fed_asin_title)

### DietPi / sensor overlays

After DietPi is installed and on Wi‑Fi, enable the overlay that matches your probe, then reboot:
DietPi docs: https://dietpi.com/docs/
```text
# DS18B20
dtoverlay=w1-gpio,gpiopin=4

# or DHT11 / DHT22
dtoverlay=dht11,gpiopin=4
```

On Bookworm-based DietPi images that file is usually `/boot/firmware/config.txt`. Older images use `/boot/config.txt`.

Install GPIO support if it is not already present:

```bash
sudo apt update
sudo apt install -y python3-gpiozero
```

Copy `tempwatch.py` onto the Pi (for example into `/root/tempwatch/tempwatch.py`).

## What the code does

Every sample interval (default **60 seconds**):

1. Reads the greenhouse probe on GPIO 4
2. Turns the **cold** relay on at **38°F** or below, and off after the temperature climbs by the hysteresis gap
3. Turns the **hot** relay on at **110°F** or above, and off after the temperature falls by the hysteresis gap
4. Fetches outdoor temperature and humidity from Open-Meteo for your lat/lon
5. Appends a row to `tempwatch.csv`
6. Serves the history graph and recent readings at **port 8787**

NOTE: Only the **greenhouse** probe temperature controls the GPIO pins. Outdoor weather is for the graph and readout only.

Edit the settings block near the top of `tempwatch.py` for pins, limits, weather location, and display timezone (default UTC−6).

## Run it on the Pi

```bash
cd ~/greenhouse
python3 tempwatch.py
```

On first run without a probe, the script simulates a room temperature and leaves the relays off so you can try the page on a laptop.

### Optional: start on boot

Example systemd unit (`/etc/systemd/system/tempwatch.service`):

```ini
[Unit]
Description=Greenhouse temperature monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=dietpi
WorkingDirectory=/root/tempwatch/
ExecStart=/usr/bin/python3 /root/tempwatch/tempwatch.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tempwatch.service
```

## Access the webpage

### On the same Wi‑Fi

Open:

```text
http://<pi-ip>:8787/
```

Find the Pi’s IP with `hostname -I` on the Pi, or in your router’s device list.

The page auto-refreshes. CSV download is at `/tempwatch.csv`.

### From anywhere (ngrok)

The Pi only listens on your LAN by default. To reach it from outside, run an [ngrok](https://ngrok.com) tunnel to port 8787.

1. Create a free ngrok account and copy your authtoken
2. On the Pi (arm architecture — Pi Zero W is 32-bit armv6; use the ngrok Linux ARM build that matches your DietPi userspace):

```bash
# example: follow current install docs from https://ngrok.com/download
ngrok config add-authtoken YOUR_TOKEN
ngrok http 8787
```

3. ngrok prints a public HTTPS URL such as `https://abc123.ngrok-free.app`. Open that URL from any network.

Keep `tempwatch.py` running while the tunnel is up. Free ngrok URLs change when you restart unless you pay for a reserved domain.

Example systemd unit for a persistent tunnel (`/etc/systemd/system/ngrok-tempwatch.service`), after `ngrok` is on your `PATH` and the authtoken is configured for the `dietpi` user:

```ini
[Unit]
Description=ngrok tunnel for greenhouse tempwatch
After=network-online.target tempwatch.service
Wants=network-online.target
Requires=tempwatch.service

[Service]
Type=simple
User=dietpi
ExecStart=/usr/local/bin/ngrok http 8787 --log=stdout
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ngrok-tempwatch.service
```

**Security note:** the page is read-only (no pin control from the browser), but anyone with the ngrok URL can see temperatures and relay state. Do not commit ngrok tokens. Prefer a reserved domain plus ngrok’s access controls if you leave this up long-term.

Alternatives if you would rather not use a public URL: [Tailscale](https://tailscale.com) or a Cloudflare Tunnel to the same port.

## License

Use and change freely for your own greenhouse.
