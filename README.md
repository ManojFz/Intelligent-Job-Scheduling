# Route Optimization System

Python route optimization system for field engineers: travel matrix (Google Maps + haversine fallback), skill-based clustering (K-Means + Hungarian assignment), and OR-Tools CVRPTW routing with break and SLA constraints.

## Setup

```bash
pip install -r requirements.txt
```

### Running on Linux

1. **Python 3.10+** (recommended). Install system deps if OR-Tools fails to build wheels (usually not needed):

   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip
   ```

2. **Copy the project** to the Linux machine (`git clone`, `scp`, `rsync`, etc.).

3. **Create a venv and install deps** (from the project root, the folder that contains `optimizer/`):

   ```bash
   cd /path/to/route-optim
   python3 -m venv .venv
   source .venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Run the API** (listens on all interfaces on port 8000):

   ```bash
   uvicorn optimizer.main:app --host 0.0.0.0 --port 8000
   ```

   For development with auto-reload:

   ```bash
   uvicorn optimizer.main:app --reload --host 0.0.0.0 --port 8000
   ```

5. **Test from another machine** (use the server’s IP or hostname):

   ```bash
   curl -s http://SERVER_IP:8000/docs
   ```

   If the firewall blocks the port:

   ```bash
   sudo ufw allow 8000/tcp
   sudo ufw reload
   ```

6. **Google Maps** (optional): distance matrix uses `optimizer/config.py` (`GOOGLE_MAPS_API_KEY`). If the key is restricted by HTTP referrer, set a server key or IP restriction for production; otherwise the API falls back to haversine distance.

## Run the API and UI

**1. Start the API** (terminal 1):
```bash
# From project root (parent of optimizer/)
uvicorn optimizer.main:app --reload --host 0.0.0.0 --port 8000
```

**2. Start the UI on port 4200** (terminal 2) — required if your Google Maps API key is restricted to `localhost:4200`:

**Option A – Node.js**  
```bash
cd frontend
npm start
```
(Or from project root: `npx serve frontend -p 4200`.)

**Option B – Python**  
```bash
cd frontend
python -m http.server 4200
```

**3. Open the app** at **http://localhost:4200**.  
The UI will call the API at `http://localhost:8000`. Do **not** open the app from port 8000 if your Maps key only allows 4200 — the map will not load.

---

The UI includes:
- **Map** – Google Map with one color per engineer route (depot → job stops).
- **Engineer cards** – Utilization bar, shift, base, timeline (depot start → jobs with travel time/distance, priority, SLA ok/risk, duration, break).
- **Unassigned jobs** – List with reasons.

Use **Load sample payload** → **Run optimization** to run the solver and render the result, or **Paste result & render** to display existing output JSON.

## Sample payload

Use **`sample_payload.json`** in the project root for a full example with:
- **break_duration_min**: 15 (gap between tickets after each job)
- **Engineers**: shift 09:00–18:00, break_window (13:00–13:30), workflows, locations (Hebbal, Whitefield, etc.)
- **Jobs**: location_name (area), workflow_type, skills, priorities, SLAs

**Constraint `config` + scoring** (see **`sample_payload_with_config.json`**):

- Each constraint: `status` = `Enabled` / `Disabled`, `type` = `Hard` / `Soft`.
- **Hard checks** (in order): skill → workflow → work location → **slot available** → **max ticket** → **travel time** `(distance_km / travel_speed_kmph) × 60` → **arrival / job_start / job_end** → **shift end** → **SLA** (if hard) → **overtime** (via shift window). SLA is evaluated only after travel and slot timing.
- **Soft score** (start 0; lower is better) + distance + travel minutes: closer base / same cluster / less travel (−5 each), far travel (+10), overtime used (+20), cluster break (+10), lower rating (+5). Optional: **`travel_speed_kmph`**, **`cluster_radius_km`**, **`preferred_override_gap`** (preferred engineer tie-break).

**From the UI:** Click **Load sample payload** then **Run optimization**.

**cURL:**
```bash
curl -X POST http://localhost:8000/optimize \
  -H "Content-Type: application/json" \
  -d @sample_payload.json
```

(Or paste the contents of `sample_payload.json` into the UI textarea.)

## Project structure

- `optimizer/main.py` – Entry point, FastAPI `POST /optimize`, orchestration
- `optimizer/travel_matrix.py` – Google Distance Matrix API (10×10 batching) + haversine fallback
- `optimizer/clustering.py` – Skill filter, K-Means, Hungarian engineer–cluster assignment
- `optimizer/router.py` – OR-Tools CVRPTW (time windows, break node, SLA, priorities)
- `optimizer/models.py` – Dataclasses for Engineer, Job, routes, output
- `optimizer/config.py` – API key and constants

## Behaviour

1. **Travel matrix**: All engineer bases + job locations; Google API with `departure_time` 09:00 IST, `traffic_model=best_guess`; fallback to haversine if the API fails. Haversine leg times enforce **≥ 60 s** when distance &gt; 0.
2. **Assignment**: Config-driven hard filters, then **soft score + travel km**; **`preferred_engineer_id`** wins unless another engineer is better by `preferred_override_gap` points.
3. **Routing**: One CVRPTW per engineer (or slot-based ordering): depot = base, jobs + mandatory break (13:00–13:30) when not slot mode; SLA windows from `config`; slot mode **utilization** = assigned jobs / total slots × 100%.
4. **Output**: JSON with `summary`, `engineer_routes`, and `unassigned` as in the spec.
