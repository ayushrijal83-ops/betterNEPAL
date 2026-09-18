# Geographic data

## PostgreSQL + PostGIS is required

Boundary geometry is stored in PostGIS `geometry(MULTIPOLYGON, 4326)` columns.
Everything else — district and municipality names, the province hierarchy,
codes, provenance — is ordinary relational data and works on any backend.

The test suite runs on in-memory SQLite. `app/gis/types.py` degrades the
geometry column to `TEXT` there so the tables can be created; **nothing spatial
works on SQLite**. Spatial behaviour is covered only by `tests/test_postgis.py`,
which skips unless a real PostGIS database is supplied.

```bash
createdb betternepal
psql -d betternepal -c "CREATE EXTENSION IF NOT EXISTS postgis;"
flask --app run.py db upgrade      # the migration creates the extension too
```

## EPSG:4326 (WGS84)

Stored coordinates are plain longitude/latitude degrees. This is what phones,
GPS units and GeoJSON already produce, so a citizen's reported location needs
no reprojection before it is stored or compared. A projected CRS would be
better for area and distance maths, but every such calculation here is done by
PostGIS, which handles it on request (`ST_Distance(geography)`) without the
whole dataset having to live in metres.

The boundary importer **rejects** a GeoJSON file whose `crs` names anything
other than EPSG:4326 rather than reprojecting it. Reprojection belongs in the
export tool that produced the file; silently guessing would put Nepal in the
sea. Per RFC 7946 an absent `crs` means WGS84 and is accepted.

## GeoJSON coordinate order

`[longitude, latitude]` — **not** `[latitude, longitude]`.

Nepal makes this error easy to miss: latitude ~27 and longitude ~85 are both
plausible numbers, and a swapped pair still parses. `Coordinates` therefore
takes keyword arguments and is the only thing passed around once input has been
parsed. Range validation alone cannot catch a swap inside Nepal's extent; see
`test_range_checks_alone_do_not_catch_a_swap_inside_nepal`.

## Hierarchy

```
Province  -> a column on District, not its own table
  District      districts.name is unique nationally
    Municipality  unique per (district_id, name)
```

Province has no attributes, geometry or references of its own in this phase, so
a table for it would be a one-column lookup joined for nothing. Promote it when
it gains real state (its own boundary, an assigned road authority, a budget).

`municipalities.district_id` is `ON DELETE RESTRICT`: deleting a district that
still holds municipalities fails rather than cascading.

## Datasets in this directory

### `districts/nepal_districts_reference.json` — reference only

All 77 districts with their province, transcribed from the project research
files and cross-checked between the two of them (both list the same 77 names
with identical province assignment).

It carries **no boundary geometry and no official codes**, and is marked
`verification_status: reference_only`. Appearing in the project's own research
files is not evidence of publication by an authoritative government source, and
the importer caps this dataset's status so it cannot promote itself.

```bash
flask --app run.py gis import-districts     # idempotent
```

### Boundary datasets — not present, must be supplied

No authoritative boundary polygons ship with this repository. Until a real
dataset is imported, `districts.boundary` stays NULL and
`GET /api/v1/map/reverse-geocode` reports `resolved: false` with reason
`no_boundary_data` — it never guesses a location.

Obtain authoritative boundaries from a body that publishes them (Survey
Department / Central Bureau of Statistics / a government open-data portal),
export as GeoJSON in EPSG:4326, and place them here:

```
app/data/districts/nepal_districts_boundaries.geojson
app/data/municipalities/nepal_municipalities_boundaries.geojson
```

Required per feature:

| field | notes |
|---|---|
| `properties.name` | must match an existing district/municipality name |
| `properties.code` | official code, **required and unique** in a boundary file |
| `properties.district` | municipalities only: parent district name |
| `properties.province` | districts only, optional |
| `geometry` | `Polygon` or `MultiPolygon`, EPSG:4326 |

Optional top-level `_meta.source` and `_meta.source_url` are recorded as
provenance. Then:

```bash
flask --app run.py gis import-district-boundaries app/data/districts/nepal_districts_boundaries.geojson
flask --app run.py gis import-municipality-boundaries app/data/municipalities/nepal_municipalities_boundaries.geojson
flask --app run.py gis coverage
```

The importer validates CRS, geometry type, required properties, duplicate codes
and missing parent relationships, and reports **every** problem at once rather
than failing on the first.

## Provenance

Every geographic row records `source`, `source_url`, `source_type`,
`verification_status` and `last_verified_at`. `verification_status` is
constrained in the database to:

| status | meaning |
|---|---|
| `unverified` | not assessed |
| `reference_only` | from project research material; names and hierarchy only |
| `official_source` | imported from an authoritative dataset, with a source URL |
| `field_verified` | confirmed on the ground by a human |

A reference import can never write `official_source`, and a dataset claiming
`official_source` without a `source_url` is rejected.

## Running the PostGIS tests

```bash
createdb betternepal_test
psql -d betternepal_test -c "CREATE EXTENSION IF NOT EXISTS postgis;"
set POSTGIS_TEST_DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/betternepal_test
python -m pytest tests/test_postgis.py -v
```

Without that variable the 13 tests skip with the reason printed by
`pytest -rs`. They are never silently counted as passing.
