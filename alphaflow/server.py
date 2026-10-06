import os
import sys
from flask import Flask, render_template, jsonify, request
from engine import run_historical_scan, get_latest_results

app = Flask(__name__)

# Force templates to reload (for development)
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/documentation')
def documentation():
    return render_template('documentation.html')

@app.route('/api/scan', methods=['POST'])
def trigger_scan():
    config = request.json or {}
    print(f"Triggering scan with config: {config}")
    try:
        results = run_historical_scan(config)
        return jsonify({"status": "success", "data": results})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/results', methods=['GET'])
def fetch_results():
    results = get_latest_results()
    return jsonify({"status": "success", "data": results})

if __name__ == '__main__':
    print("🚀 Starting AlphaFlow Server on port 5001...")
    # Run on port 5001
    # The debugger runs code from the browser, so it stays off unless asked for (ALPHAFLOW_DEBUG=1) and then
    # only listens on this machine.
    debug = os.environ.get("ALPHAFLOW_DEBUG") == "1"
    app.run(host='127.0.0.1' if debug else '0.0.0.0', port=5001, debug=debug)
