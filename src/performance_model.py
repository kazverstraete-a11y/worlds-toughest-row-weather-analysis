import numpy as np
import pandas as pd
import glob 
import json
import re
import math
import xml.etree.ElementTree as ET
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Patch
from matplotlib import colors as mcolor
from pathlib import Path
from datetime import datetime
from datetime import timezone
from datetime import timedelta
import statsmodels.api as sm



#--- Parse all leaderboards into Python dictionary 
json_files = glob.glob('data/*.json') 

leaderboards_unsorted = {}
snapshots ={}

for file_path in json_files:
    match = re.search(r"(\d{4}-\d{2}-\d{2})\.[^.]+$", file_path)
    date_key = match.group(1)
    
    mtime = datetime.fromtimestamp(Path(file_path).stat().st_mtime, tz=timezone.utc)
    snapshots[date_key] = mtime
    
    with open (file_path, 'r') as f:
        data = json.load(f)
        overall_rank = data["tags"][0]["teams"]
        leaderboards_unsorted[date_key] = overall_rank

leaderboards = dict(sorted(
                leaderboards_unsorted.items(), 
                key=lambda item: datetime.strptime(item[0], "%Y-%m-%d")
))

#--- Select data & DF construct ---
KEEP = ['id', 'd24', 'dmg','finished']
rows=[]
for date, boats in leaderboards.items():
    for b in boats:
        if b.get("finished", False):
            continue
        rows.append({k: b.get(k) for k in KEEP} | {'date': date})
        
df = pd.DataFrame(rows)
df['date'] = pd.to_datetime(df['date'])
df["snapshot_ts"] = pd.to_datetime(
    df["date"].dt.strftime("%Y-%m-%d").map(snapshots),
    utc=True
)
df["slot_4h"] = df["snapshot_ts"].dt.floor("4h")
df["wx_slot"] = df["slot_4h"]

df = df.sort_values(['date', 'id']).set_index('date')
df['d24_km'] = df['d24'] / 1000

#--- CALCULATE INDIVIDUAL LOCATION AT T-12 (MIDPOINT) ---
#--- KML of the route ---
def read_kml_coordinates(kml_path):
    tree = ET.parse(kml_path)
    root = tree.getroot()
    
    coords_element = root.find(".//{*}coordinates")
    if coords_element is None or not coords_element.text:
        raise ValueError("Geen coordinates gevonden in het KML-bestand")
        
    raw = coords_element.text.strip()
    
    points = list()
    for chunk in raw.replace("\n", " ").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        lon = float(parts[0])
        lat = float(parts[1])
        points.append((lat, lon))
    
    if len(points) < 2:
        raise ValueError(f"Te weinig punten gevonden: {len(points)}")
        
    return points

coordinates = read_kml_coordinates('route.kml')
route_df = pd.DataFrame(coordinates, columns=['lat', 'lon'])

boats_per_day = df.groupby(level=0)["id"].nunique()
print(boats_per_day.head(), boats_per_day.tail())

#--- Cumulative distance along KML route ---
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    
    return 2 * R * math.asin(math.sqrt(a))

lat_prev = route_df['lat'].shift(1)
lon_prev = route_df['lon'].shift(1)

route_df['seg_km'] = [
    np.nan if pd.isna(a) else haversine_km(a, b, c, d)
    for a, b, c, d in zip(lat_prev, lon_prev, route_df['lat'], route_df['lon'])
]

route_df['seg_km'] = route_df['seg_km'].fillna(0.0)
route_df['cum_km'] = route_df['seg_km'].cumsum()

#--- #segment of the KML
def find_segment(route_df, dmg_km):
    """
    Zoekt i zodat cum_km[i] <= dmg_km < cum_km[i + 1]
    Geeft index i terug.
    """
    #safety voor moest dmg_km voorbij het einde zijn
    max_km = route_df['cum_km'].iloc[-1]
    if dmg_km >= max_km:
        dmg_km = max_km -1e-9
    if dmg_km < 0:
        raise ValueError("Dmg_km is negatief")
        
    i = route_df[route_df['cum_km'] <= dmg_km].index.max()
    if i is None or i == len(route_df)-1:
        raise ValueError("Kon geen geldig segment vinden (dmg_km buiten route?).")
    return i, dmg_km

#interpolate position
def interpolate_position(route_df, dmg_km):
    
    """
    Interpoleert positie op basis van dmg_km (totale afstand sinds start).
    Returns: (lat, lon, i) waarbij i het segment is.
    """
    
    i, dmg_km = find_segment(route_df, dmg_km)
    km0 = route_df.loc[i, 'cum_km']
    km1 = route_df.loc[i + 1, 'cum_km']
    
    t = (dmg_km - km0) / (km1 - km0) #fractie binnen het segment 
    
    lat0, lon0 = route_df.loc[i, ['lat', 'lon']]
    lat1, lon1 = route_df.loc[i + 1, ['lat', 'lon']]
    
    lat = lat0 + t *(lat1 - lat0)
    lon = lon0 + t * (lon1 - lon0)
    
    return lat, lon, i

def bearing_deg(lat1, lon1, lat2, lon2):
    """Bearing in graden (0-360): richting van punt1 naar punt2 """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    
    y = math.sin(dlambda) * math.cos(phi2)
    x = (math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda))

    brng = math.degrees(math.atan2(y, x))
    return (brng + 360) % 360

def position_and_bearing_from_dmg(route_df, dmg_km):
    """
    Convenience: in één call positie + bearing.
    Returns: (lat, lon, bearing, segment_index)
    """
    lat, lon, i = interpolate_position(route_df, dmg_km)
    brng = bearing_deg(
        route_df.loc[i, 'lat'], route_df.loc[i, 'lon'],
        route_df.loc[i + 1, 'lat'], route_df.loc[i + 1, 'lon']
    )
    return lat, lon, brng, i

df['dmg_km'] = df['dmg'] / 1000
df['start_km'] = (df['dmg'] - df['d24']) / 1000
df['dmg_midpoint'] = 0.5 * (df['dmg_km'] + df['start_km'])

tmp = df['dmg_midpoint'].apply(lambda x: position_and_bearing_from_dmg(route_df, x))
df[['mid_lat', 'mid_lon', 'bearing','seg_i']] = pd.DataFrame(tmp.tolist(), index=df.index)

df["wx_seg"]  = df["seg_i"].astype("int64")

#key per day and api call per day
df['day'] = df.index.normalize()
rep = (
    df.sort_index()
      .groupby(["wx_slot","wx_seg"], sort=False)
      .head(1)
      .copy()
)

# Load local weather data (only once)
with open("weather_by_location.json", "r") as f:
    LOCAL_WEATHER_DATA = json.load(f)

# Helper to find closest available coordinate (rounding to 4 decimals)
def find_nearest_coord_key(lat, lon, data_keys):
    lat = round(lat, 4)
    lon = round(lon, 4)
    key = f"{lat}_{lon}"
    if key in data_keys:
        return key
    # fallback: find closest match manually if rounding is inconsistent
    distances = {
        k: (float(k.split("_")[0]) - lat)**2 + (float(k.split("_")[1]) - lon)**2
        for k in data_keys
    }
    return min(distances, key=distances.get)


def get_weather_from_json(lat, lon, center_datetime_utc):
    key = find_nearest_coord_key(lat, lon, LOCAL_WEATHER_DATA.keys())
    data = LOCAL_WEATHER_DATA[key]

    # Parse marine and wind hourly into dataframes
    df_marine = pd.DataFrame(data["marine_hourly"])
    df_wind = pd.DataFrame(data["wind_hourly"])

    # Ensure datetime index
    df_marine["time"] = pd.to_datetime(df_marine["time"], utc=True)
    df_wind["time"] = pd.to_datetime(df_wind["time"], utc=True)
    df_marine = df_marine.set_index("time")
    df_wind = df_wind.set_index("time")

    # Filter 24h window before the given slot
    start = center_datetime_utc - timedelta(hours=24)
    end = center_datetime_utc
    mar_hourly_df = df_marine.loc[start:end].tail(24)
    wind_hourly_df = df_wind.loc[start:end].tail(24)
    
    #nan
    mar_hourly_df = mar_hourly_df.ffill().bfill()
    wind_hourly_df = wind_hourly_df.ffill().bfill()

    # --- Marine stats ---
    wave_height_med = mar_hourly_df["wave_height"].median()
    wave_height_p90 = mar_hourly_df["wave_height"].quantile(0.9)
    wave_period_med = mar_hourly_df["wave_period"].median()
    ocean_current_v = mar_hourly_df["ocean_current_velocity"]
    current_speed_med = ocean_current_v.median()
    ocean_current_dir = mar_hourly_df["ocean_current_direction"]

    # --- Wind stats ---
    wind_speed_med = wind_hourly_df["wind_speed_10m"].median()
    wind_gusts_p90 = wind_hourly_df["wind_gusts_10m"].quantile(0.9)

    return {
        "marine_df": mar_hourly_df,
        "wind_df": wind_hourly_df,
        "wave_height_med": float(wave_height_med),
        "wave_height_p90": float(wave_height_p90),
        "wave_period_med": float(wave_period_med),
        "current_speed_med": float(current_speed_med),
        "wind_speed_med": float(wind_speed_med),
        "wind_gusts_p90": float(wind_gusts_p90),
        "current_dir_series": ocean_current_dir,
        "current_v_series": ocean_current_v,
        "wind_dir_series": wind_hourly_df["wind_direction_10m"],
        "wind_speed_series": wind_hourly_df["wind_speed_10m"]
    }

def get_local_weather_stats(row):
    lat = row["mid_lat"]
    lon = row["mid_lon"]
    course_deg = row["bearing"]
    center = pd.to_datetime(row["slot_4h"], utc=True)

    wx = get_weather_from_json(lat, lon, center)

    def wrap180(deg): return (deg + 180) % 360 - 180

    # --- Current vectors ---
    delta_mar = wrap180(wx["current_dir_series"] - course_deg)
    along_current = wx["current_v_series"] * np.cos(np.deg2rad(delta_mar))
    cross_current = wx["current_v_series"] * np.abs(np.sin(np.deg2rad(delta_mar)))
    tail_current = np.clip(along_current, 0, None)
    head_current = np.clip(-along_current, 0, None)

    # --- Wind vectors ---
    wind_to = (wx["wind_dir_series"] + 180) % 360
    delta_wind = wrap180(wind_to - course_deg)
    wind_ms = wx["wind_speed_series"] / 3.6
    along_wind = wind_ms * np.cos(np.deg2rad(delta_wind))
    cross_wind = wind_ms * np.abs(np.sin(np.deg2rad(delta_wind)))
    tail_wind = np.clip(along_wind, 0, None)
    head_wind = np.clip(-along_wind, 0, None)
    
    def safe_nanmedian(arr):
        return float(np.nanmedian(arr)) if np.any(~np.isnan(arr)) else np.nan

    # --- Final output ---
    return {
        "wave_height_med": wx["wave_height_med"],
        "wave_height_p90": wx["wave_height_p90"],
        "wave_period_med": wx["wave_period_med"],
        "current_speed_med": wx["current_speed_med"],
        "current_along_med": safe_nanmedian(along_current),
        "current_tail_med":  safe_nanmedian(tail_current),
        "current_head_med":  safe_nanmedian(head_current),
        "current_cross_med": safe_nanmedian(cross_current),
        "wind_speed_med": wx["wind_speed_med"],
        "wind_gusts_p90": wx["wind_gusts_p90"],
        "wind_along_med":    safe_nanmedian(along_wind),
        "wind_tail_med":     safe_nanmedian(tail_wind),
        "wind_head_med":     safe_nanmedian(head_wind),
        "wind_cross_med":    safe_nanmedian(cross_wind),
    }

rep_weather = rep.apply(get_local_weather_stats, axis=1, result_type="expand")
rep[rep_weather.columns] = rep_weather


df = df.join(
    rep.set_index(["wx_slot", "wx_seg"])[rep_weather.columns],
    on=["wx_slot", "wx_seg"]
)

#---- RACE WIDE DATAFRAME ----
race_daily = (
    df
    .groupby(df.index)
    .median(numeric_only=True)
)

start_date = pd.to_datetime("2025-12-14")
race_daily['start_date'] = start_date
race_daily['days_at_sea'] = (race_daily.index - race_daily['start_date']).dt.days
race_daily = race_daily[race_daily["days_at_sea"] >= 8]

model_features = [
    "wave_height_p90",
    "current_cross_med",
    "wind_cross_med",
    "current_along_med",
]

# Stap 1: Maak X en y
X = race_daily[model_features].copy()
y = race_daily["d24_km"]

# Stap 2: Drop NA tegelijk
data = pd.concat([X, y], axis=1).dropna()
X_clean = data[model_features]
y_clean = data["d24_km"]

# Stap 3: Voeg constant toe
X_with_const = .add_constant(X_clean)

# Stap 4: Fit
model = sm.OLS(y_clean, X_with_const).fit()

# Stap 5: Predict op dezelfde data
race_daily.loc[X_clean.index, "d24_pred_final"] = model.predict(X_with_const).round(1)


#residuals
race_daily["residual"] = race_daily["d24_km"] - race_daily["d24_pred_final"]

# --- VISUALISATIE ---

rd = race_daily.sort_index().copy()

fig, ax = plt.subplots(figsize=(14, 6))

# Observed vs expected
ax.plot(rd.index, rd["d24_km"], lw=1.5, color='#1f77b4', label="Observed d24 (km)")
ax.plot(rd.index, rd["d24_pred_final"], lw=1.5, color='#888888', label="Predicted d24 (based on conditions)")

# Shaded performance residuals
above = rd["residual"] > 0
below = rd["residual"] < 0

above_color = '#8fd694'   
below_color = '#f19c99'

ax.fill_between(rd.index, rd["d24_km"], rd["d24_pred_final"], where=above, interpolate=True,
                color=above_color, alpha=0.5)
ax.fill_between(rd.index, rd["d24_km"], rd["d24_pred_final"], where=below, interpolate=True,
                color=below_color, alpha=0.5)

# Annotaties en periodes
ax.axvline(pd.to_datetime("2025-12-28"), color='gray', linestyle=':', lw=1.5)
ax.axvline(pd.to_datetime("2026-01-07"), color='gray', linestyle=':', lw=1.5)

ax.axvspan(pd.to_datetime("2025-12-21"), pd.to_datetime("2025-12-28"), color='mediumseagreen', alpha=0.05)
ax.axvspan(pd.to_datetime("2025-12-28"), pd.to_datetime("2026-01-07"), color='orangered', alpha=0.05)
ax.axvspan(pd.to_datetime("2026-01-07"), pd.to_datetime("2026-01-20"), color='mediumseagreen', alpha=0.05)

ax.grid(axis='y', linestyle='--', alpha=0.3)

shading_handles = [
    Patch(facecolor=above_color, edgecolor=above_color, alpha=0.4, label='Above expected performance'),
    Patch(facecolor=below_color, edgecolor=below_color, alpha=0.4, label='Below expected performance'),
]

# Eerste legende (shading)
leg1 = ax.legend(handles=shading_handles, loc='lower right', fontsize=9, title='Shading')

# Tweede legende (standaard lijnen, rechtsonder)
leg2 = ax.legend(loc='lower left', fontsize=9, frameon=True, title='Plotlines')
ax.add_artist(leg1)

# Layout en stijl
ax.set_title(
    "World’s Toughest Row (2026):\nPerformance vs Weather-Based Expectations Highlights Human Adaptation",
    fontsize=15, fontweight='demibold'
)

ax.set_ylabel("Median 24-hour distance (km)", fontsize=12, labelpad=15, fontweight='bold')
ax.set_ylim(80, 150)
ax.xaxis.set_major_locator(.DayLocator(interval=2))
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
ax.tick_params(axis='x', labelsize=10, rotation=30)
ax.tick_params(axis='y', labelsize=10)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)

# Fase 1: sterke start
ax.annotate("Weeks 1-2:\n\nFast start and consolidation", xy=(rd.index[0], 120), xytext=(rd.index[0], 140), color='black', fontsize=10)

# Fase 2: dip in prestatie
ax.annotate("Weeks 3-4:\n\nFatigue sets in", xy=(rd.index[6], 120), xytext=(rd.index[9], 140), color='black', fontsize=10)

# Fase 3: aanpassing
ax.annotate("Weeks following:\n\nAdaptation to the extreme conditions allows max performance",xy=(rd.index[17], 120), xytext=(rd.index[17], 140), color='black', fontsize=10)

plt.rcParams['font.family'] = 'DejaVu Sans'


plt.tight_layout()
plt.show()

