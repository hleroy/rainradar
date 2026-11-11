"""The 1-hour rain forecast (Météo-France PIAF) — the fifth failure domain.

See ``specs/forecast-nowcast.md``. :mod:`radar.forecast.grib_decode` and
:mod:`radar.forecast.render` are pure functions (decode one PIAF response, mask it to
the observation's radar coverage, render the tile matrix); :mod:`radar.forecast.piaf`
fetches, :mod:`radar.forecast.job` schedules, stores and publishes a run, and
:mod:`radar.forecast.advert` decides which of its frames ``/api/radar/frames`` shows.
"""
