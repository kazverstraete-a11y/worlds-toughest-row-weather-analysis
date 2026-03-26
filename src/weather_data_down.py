import json
import time
import openmeteo_requests
import pandas as pd
import xml.etree.ElementTree as ET
import requests_cache
from retry_requests import retry

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

#variables
coordinates = read_kml_coordinates('route.kml')

# Set up API client with cache and retry
cache_session = requests_cache.CachedSession(".cache", expire_after=3600)
retry_session = retry(cache_session, retries=3, backoff_factor=0.2)
openmeteo = openmeteo_requests.Client(session=retry_session)

weather_data = {}
# Loop through coordinates and fetch data
for lat, lon in coordinates:  
    key = f"{lat}_{lon}"
    try:
        # --- Marine ---
        marine_url = "https://marine-api.open-meteo.com/v1/marine"
        marine_params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "wave_height", "wave_period", "wave_direction",
                "ocean_current_velocity", "ocean_current_direction"
            ],
            "past_days": 44,
            "forecast_days": 1,
            "timezone": "UTC"
        }
        marine_response = openmeteo.weather_api(marine_url, params=marine_params)[0]
        marine_hourly = marine_response.Hourly()
        marine_data = {
            "time": pd.date_range(
            start=pd.to_datetime(marine_hourly.Time(), unit="s", utc=True),
            periods=marine_hourly.Variables(0).ValuesAsNumpy().shape[0],
            freq=pd.Timedelta(seconds=marine_hourly.Interval())
            ).astype(str).tolist(),
            "wave_height": marine_hourly.Variables(0).ValuesAsNumpy().tolist(),
            "wave_period": marine_hourly.Variables(1).ValuesAsNumpy().tolist(),
            "wave_direction": marine_hourly.Variables(2).ValuesAsNumpy().tolist(),
            "ocean_current_velocity": marine_hourly.Variables(3).ValuesAsNumpy().tolist(),
            "ocean_current_direction": marine_hourly.Variables(4).ValuesAsNumpy().tolist(),
            }
        # --- Wind ---
        wind_url = "https://api.open-meteo.com/v1/forecast"
        wind_params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": [
                "wind_direction_10m", "wind_speed_10m", "wind_gusts_10m"
            ],
            "past_days": 40,
            "forecast_days": 1,
            "timezone": "UTC"
        }
        wind_response = openmeteo.weather_api(wind_url, params=wind_params)[0]
        wind_hourly = wind_response.Hourly()
        
        wind_data = {
            "time": pd.date_range(
            start=pd.to_datetime(wind_hourly.Time(), unit="s", utc=True),
            periods=wind_hourly.Variables(0).ValuesAsNumpy().shape[0],
            freq=pd.Timedelta(seconds=wind_hourly.Interval())
            ).astype(str).tolist(),
            "wind_direction_10m": wind_hourly.Variables(0).ValuesAsNumpy().tolist(),
            "wind_speed_10m": wind_hourly.Variables(1).ValuesAsNumpy().tolist(),
            "wind_gusts_10m": wind_hourly.Variables(2).ValuesAsNumpy().tolist(),
            }

        weather_data[key] = {
            "lat": lat,
            "lon": lon,
            "marine_hourly": marine_data,
            "wind_hourly": wind_data,
        }

        print(f"Fetched data for {key}")

        time.sleep(0.25)  # polite delay between calls

    except Exception as e:
        print(f"Error fetching for {key}: {e}")

# Save to JSON
with open("weather_by_location.json", "w") as f:
    json.dump(weather_data, f, indent=2)

print("✅ Weather data saved to weather_by_location.json")
