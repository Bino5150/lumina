# Mock weather report generator
# Returns structured JSON with temperature, conditions, humidity, wind

def get_weather(city: str) -> dict:
    """
    Generate a mock weather report for the given city.
    Uses simple hash-based randomness to vary results per city.
    """
    import json
    
    # Simple hash function for consistent but varied results
    def hash_string(s):
        h = 0
        for char in s:
            h = (h * 31 + ord(char)) & 0xFFFFFFFF
        return h
    
    city_hash = hash_string(city)
    seed = city_hash % 256
    
    # Generate mock weather data
    temp_min = int((seed % 24) - 5)
    temp_max = int(((seed + 7) % 28) - 3)
    humidity = (seed * 137) % 90
    wind_speed = ((seed + 19) % 20) + 2
    
    # Determine weather condition based on hash
    cond_hash = (city_hash >> 4) & 0x3F
    conditions = ["Sunny", "Cloudy", "Rainy", "Snowy", "Windy"]
    condition = conditions[cond_hash % 5]
    
    # Return structured JSON
    return {
        "city": city,
        "temperature_celsius": temp_min, "temperature_max_celsius": temp_max,
        "humidity_percent": humidity,
        "wind_speed_kmh": wind_speed,
        "condition": condition
    }

def register_get_weather_tool(registry):
    registry.register(
        name="get_weather",
        fn=get_weather,
        description="Returns a mock weather report for any city given as input.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "Name of the city."}
            },
            "required": ["city"]
        }
    )

if __name__ == "__main__":
    # Test it directly
    result = get_weather("Paris")
    print(json.dumps(result, indent=2))
