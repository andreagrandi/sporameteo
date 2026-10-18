-- Fingerprints of the grid, forest and terrain values of each region in one set of builds.
-- Set grid, forest and terrain to the build versions first. The national builds and the
-- copied regions-all builds must print the same lines. Row ids and timestamps are left out
-- because the copy gives them new values. The regions part is materialized so that each
-- region outline is hashed once. Hashing it for every square takes hours.
\set QUIET on
\pset tuples_only on
\pset format unaligned
with regions as materialized (
  select r.id, r.istat_code, concat_ws('|', r.name, md5(st_asewkb(r.geometry)::text)) as region
  from national_grid_gridregion r
  where r.id in (
    select distinct m.owner_region_id
    from national_grid_gridbuildcell m
    join national_grid_gridbuild b on b.id = m.build_id and b.version = :'grid'
  )
),
squares as (
  select r.istat_code as region, r.region as outline, m.cell_id,
    concat_ws('|', m.cell_id, m.land_share, md5(st_asewkb(m.land_geometry)::text),
      c.corner_e, c.corner_n, c.municipality_id, md5(st_asewkb(c.geometry)::text)) as square,
    concat_ws('|', f.total_pixel_count, f.valid_pixel_count, f.forest_pixel_count,
      f.broadleaf_pixel_count, f.conifer_pixel_count, f.unclassifiable_pixel_count,
      f.outside_pixel_count, f.valid_coverage, f.coverage_quality, f.forest_share,
      f.broadleaf_share, f.leaf_type, f.is_forest, f.missing_reason, f.weather_latitude,
      f.weather_longitude) as forest,
    concat_ws('|', t.altitude_m, t.slope_deg, t.aspect_deg, t.altitude_valid_fraction,
      t.slope_valid_fraction, t.aspect_valid_fraction, t.altitude_quality, t.slope_quality,
      t.aspect_quality, t.source_gap, t.source_gap_detail) as terrain
  from national_grid_gridbuildcell m
  join national_grid_gridbuild b on b.id = m.build_id and b.version = :'grid'
  join regions r on r.id = m.owner_region_id
  join national_grid_gridcell c on c.cell_id = m.cell_id
  left join national_forest_forestcell f on f.grid_build_cell_id = m.id
    and f.build_id = (select id from national_forest_forestbuild where version = :'forest')
  left join national_terrain_terraincell t on t.grid_build_cell_id = m.id
    and t.build_id = (select id from national_terrain_terrainbuild where version = :'terrain')
)
select region, outline, count(*),
  md5(string_agg(square, ';' order by cell_id)),
  md5(string_agg(forest, ';' order by cell_id)),
  md5(string_agg(terrain, ';' order by cell_id))
from squares group by region, outline order by region;
