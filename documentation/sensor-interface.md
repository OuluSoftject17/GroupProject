# Public API of the Legacy Application
The legacy application is a combined temperature and humidity sensor for sauna environments.
As we do not have the physical hardware, we will simulate it.

## HTTP(S) Interfaces
The application hosts a web server with two public endpoints

### Health
At `/health`, the application will always answer with status 200 and a plaintext "OK". This can be used to verify service liveness.

### Measurements
At `/` and/or `/measurements` the application answers with a JSON blob containing the current temperature and relative humidity. As
we are speaking of a simulator, these values will be random to a degree.

Example:
```json
{
  "temperatureC":86,
  "humidity%":47
}
```
