-- Fingerprints of the Tuscany inputs and the published Tuscany run, and the database size.
-- Run it before and after loading other regions. Every line except size must stay the same.
\set QUIET on
\pset tuples_only on
\pset format unaligned
select 'builds', md5(string_agg(v, '|' order by v)) from (
  select row(b.*)::text v from national_grid_gridbuild b where version like 'tuscany-%'
  union all select row(b.*)::text from national_forest_forestbuild b where version like 'tuscany-%'
  union all select row(b.*)::text from national_terrain_terrainbuild b where version like 'tuscany-%') t;
select 'squares', count(*), md5(string_agg(row(m.*)::text || row(c.*)::text, '|' order by m.id))
  from national_grid_gridbuildcell m join national_grid_gridbuild b on b.id = m.build_id
  join national_grid_gridcell c on c.cell_id = m.cell_id where b.version like 'tuscany-%';
select 'forest', count(*), md5(string_agg(row(f.*)::text, '|' order by f.id))
  from national_forest_forestcell f join national_forest_forestbuild b on b.id = f.build_id
  where b.version like 'tuscany-%';
select 'terrain', count(*), md5(string_agg(row(t.*)::text, '|' order by t.id))
  from national_terrain_terraincell t join national_terrain_terrainbuild b on b.id = t.build_id
  where b.version like 'tuscany-%';
select 'input_set', count(*), md5(string_agg(row(i.*)::text, '|' order by i.id))
  from estimate_updates_tuscanyinputset i where forest_build_version like 'tuscany-%';
select 'host_trees', count(*), md5(string_agg(row(h.*)::text, '|' order by h.id))
  from estimate_updates_hosttreecache h where h.cell_id in (
    select m.cell_id from national_grid_gridbuildcell m join national_grid_gridbuild b on b.id = m.build_id
    where b.version like 'tuscany-%');
select 'published', md5(string_agg(row(p.*)::text, '|' order by p.id)) from estimate_updates_publishedforecastrun p;
select 'results', count(*), md5(string_agg(r.forest_cell_id::text || ':' || encode(r.values, 'hex'), '|' order by r.forest_cell_id))
  from estimate_updates_estimateresult r join estimate_updates_forecastruncell c on c.update_id = r.update_id
  join estimate_updates_publishedforecastrun p on p.current_run_id = c.run_id where p.region_code = '09';
select 'size', pg_database_size(current_database());
