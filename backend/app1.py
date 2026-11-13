from flask import Flask, render_template_string, jsonify, request
import requests

app = Flask(__name__)

# TODO: Replace with the actual IP address shown in your ESP32 Serial Monitor
ESP32_IP = "10.98.111.148" 

# Coordinates for Mangaluru, Karnataka, India
LATITUDE = "12.9141"
LONGITUDE = "74.8560"

def get_mangaluru_weather():
    """Fetches live temperature and humidity for Mangaluru via Open-Meteo (No API key required)"""
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={LATITUDE}&longitude={LONGITUDE}&current=temperature_2m,relative_humidity_2m"
        response = requests.get(url, timeout=3)
        if response.status_code == 200:
            current = response.json().get('current', {})
            temp = current.get('temperature_2m', 0.0)
            hum = current.get('relative_humidity_2m', 0.0)
            return temp, hum
    except Exception:
        pass
    return 0.0, 0.0

# HTML Template for the Laptop Dashboard
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ESP32 IoT Laptop Dashboard</title>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f0f2f5; text-align: center; margin: 0; padding: 20px; color: #333; }
        .container { max-width: 650px; margin: auto; background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.1); }
        h2 { color: #007bff; margin-bottom: 5px; }
        .subtitle { color: #666; font-size: 14px; margin-bottom: 20px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 20px; }
        .card { background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 5px solid #007bff; text-align: left; }
        .card.alert { border-left-color: #28a745; }
        .card h3 { margin: 0 0 8px 0; font-size: 16px; color: #495057; }
        .card p { margin: 0; font-size: 18px; font-weight: bold; color: #212529; }
        .section-title { font-size: 18px; margin: 20px 0 10px 0; text-align: left; border-bottom: 2px solid #e9ecef; padding-bottom: 5px; color: #343a40; }
        .btn-group { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 15px; }
        .btn { flex: 1; padding: 12px; font-size: 14px; font-weight: bold; color: white; background: #6c757d; border: none; border-radius: 6px; cursor: pointer; text-decoration: none; text-align: center; transition: 0.2s; }
        .btn:hover { opacity: 0.9; }
        .btn.active { background: #28a745; }
        .btn-on { background: #28a745; }
        .btn-off { background: #dc3545; }
        .btn-auto { background: #17a2b8; }
    </style>
    <script>
        function updateDashboard() {
            fetch('/get_sensor_data')
                .then(response => response.json())
                .then(data => {
                    if(data.error) {
                        document.getElementById('connection-status').innerText = "Disconnected from ESP32!";
                        document.getElementById('connection-status').style.color = "red";
                        return;
                    }
                    document.getElementById('connection-status').innerText = "Connected Live";
                    document.getElementById('connection-status').style.color = "green";
                    
                    document.getElementById('mode').innerText = data.mode.toUpperCase();
                    document.getElementById('pir').innerText = data.pir == 1 ? 'Motion Detected' : 'Clear';
                    document.getElementById('ldrVal').innerText = data.ldrVal;
                    document.getElementById('ldrState').innerText = data.ldrState;
                    document.getElementById('temp').innerText = data.temp + ' °C (Mangaluru)';
                    document.getElementById('hum').innerText = data.hum + ' %';
                    document.getElementById('led').innerText = data.led;
                    document.getElementById('relay').innerText = data.relay;
                }).catch(err => {
                    document.getElementById('connection-status').innerText = "Error connecting to backend/ESP32";
                    document.getElementById('connection-status').style.color = "red";
                });
        }
        setInterval(updateDashboard, 1500);
    </script>
</head>
<body>
    <div class="container">
        <h2>ESP32 Laptop Control Center</h2>
        <div class="subtitle">Status: <span id="connection-status" style="font-weight:bold; color: orange;">Connecting...</span></div>

        <!-- Operating Mode Controls -->
        <div class="section-title">Automatic Operating Mode</div>
        <div class="btn-group">
            <a href="/set_mode?mode=pir" class="btn {% if mode == 'pir' %}active{% endif %}">PIR Motion Mode</a>
            <a href="/set_mode?mode=ldr" class="btn {% if mode == 'ldr' %}active{% endif %}">LDR Light Mode</a>
        </div>

        <!-- Manual Override Controls -->
        <div class="section-title">Manual Overrides & Control</div>
        
        <p style="text-align: left; font-size: 14px; font-weight: bold; margin: 5px 0;">LED Manual Control:</p>
        <div class="btn-group">
            <a href="/control?device=led&action=on" class="btn btn-on">Turn LED ON</a>
            <a href="/control?device=led&action=off" class="btn btn-off">Turn LED OFF</a>
            <a href="/control?device=led&action=auto" class="btn btn-auto">LED Auto</a>
        </div>

        <p style="text-align: left; font-size: 14px; font-weight: bold; margin: 5px 0;">Relay / Fan Manual Control:</p>
        <div class="btn-group">
            <a href="/control?device=relay&action=on" class="btn btn-on">Turn Relay ON</a>
            <a href="/control?device=relay&action=off" class="btn btn-off">Turn Relay OFF</a>
            <a href="/control?device=relay&action=auto" class="btn btn-auto">Relay Auto</a>
        </div>

        <!-- Live Sensor Grid -->
        <div class="section-title">Live Sensor Dashboard</div>
        <div class="grid">
            <div class="card">
                <h3>Operating Mode</h3>
                <p id="mode">--</p>
            </div>
            <div class="card">
                <h3>PIR Motion</h3>
                <p id="pir">--</p>
            </div>
            <div class="card">
                <h3>LDR Reading</h3>
                <p><span id="ldrVal">0</span> (<span id="ldrState">--</span>)</p>
            </div>
            <div class="card">
                <h3>Temperature</h3>
                <p id="temp">-- °C</p>
            </div>
            <div class="card">
                <h3>Humidity</h3>
                <p id="hum">-- %</p>
            </div>
            <div class="card">
                <h3>LED Status</h3>
                <p id="led">--</p>
            </div>
            <div class="card" style="grid-column: span 2;">
                <h3>Relay (Fan) Status</h3>
                <p id="relay">--</p>
            </div>
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, mode="pir")

@app.route('/get_sensor_data')
def get_sensor_data():
    try:
        # Fetch data from ESP32
        response = requests.get(f"http://{ESP32_IP}/data", timeout=2)
        data = response.json()
        
        # Override ESP32 temperature/humidity with live Mangaluru location data
        m_temp, m_hum = get_mangaluru_weather()
        data['temp'] = round(m_temp, 1)
        data['hum'] = round(m_hum, 1)
        
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/set_mode')
def set_mode():
    mode = request.args.get('mode', 'pir')
    try:
        requests.get(f"http://{ESP32_IP}/setMode?mode={mode}", timeout=2)
    except:
        pass
    return render_template_string(HTML_TEMPLATE, mode=mode)

@app.route('/control')
def control():
    device = request.args.get('device')  # 'led' or 'relay'
    action = request.args.get('action')  # 'on', 'off', or 'auto'
    try:
        requests.get(f"http://{ESP32_IP}/control?device={device}&action={action}", timeout=2)
    except:
        pass
    return render_template_string(HTML_TEMPLATE)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)