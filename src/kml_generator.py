from geographiclib.geodesic import Geodesic

start = (28.077419, -17.328871)   #Lagomera
end   = (17.003017, -61.763289)  #Antigua

geod = Geodesic.WGS84
line = geod.InverseLine(start[0], start[1], end[0], end[1])

points = []
step = 10000  # 10 km
n = int(line.s13 // step)

for i in range(n + 1):
    pos = line.Position(i * step)
    points.append((pos['lon2'], pos['lat2']))
    

with open("route.kml", "w") as f:
    f.write("""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<Placemark>
<LineString>
<tessellate>1</tessellate>
<coordinates>
""")
    for lon, lat in points:
        f.write(f"{lon},{lat},0\n")
    f.write("""</coordinates>
</LineString>
</Placemark>
</Document>
</kml>""")
