import sys
from pathlib import Path
from flask import Flask, render_template, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the project's config.py
import config  # noqa: E402
from engine import run_historical_scan, get_latest_results  # noqa: E402

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
    params = request.json or {}
    print(f"Triggering scan with config: {params}")
    try:
        results = run_historical_scan(params)
        return jsonify({"status": "success", "data": results})
    except RuntimeError as e:
        # The scan could not run (no key, Databento refused): a plain message, stored results untouched.
        return jsonify({"status": "error", "message": str(e)}), 503
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
    debug = config.ALPHAFLOW_DEBUG
    app.run(host='127.0.0.1' if debug else '0.0.0.0', port=5001, debug=debug)
