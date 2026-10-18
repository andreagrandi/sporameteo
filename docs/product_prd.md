# SporaMeteo product requirements

Written on 11 October 2026.

These product requirements follow the completed ten-question interview. This document records the owner's MVP choices and the later feature list. It is the source for the next epics and tickets.

## Product purpose

SporaMeteo helps people choose areas to explore for porcini. It combines terrain, habitat and weather information in a map that shows the estimated probability that porcini are growing in an area on a selected day.

The first release is a free website without advertising or subscriptions. It works on phones and desktop computers, covers all 20 Italian regions under D24, and supports Italian and English. It shows today and the next seven days.

The app is an outdoor companion. The owner accepts experimental probability estimates at launch. They must carry a clear explanation that their accuracy has not been validated. The product must not promise exact mushroom locations or a successful outing. Launch does not depend on the owner visiting locations, recording finds or checking predictions in the field.

A polished interface is a launch requirement. A small feature set does not justify an unfinished appearance or awkward interactions.

## Which decisions govern this release

Epic 1 investigated technical possibilities. Its experiments and reports supply evidence, reusable work and known limits. They do not select the app's features. This PRD takes precedence over their suggestions about the first public release.

In particular, the interview set all Italy as the MVP coverage rather than the investigation's home area. D22 later limited the first version to Tuscany, and D24 brought it back to all 20 regions. It presents experimental growing-probability estimates. Validation against field observations is not a release condition. The historical reports and their results remain unchanged.

Decisions D15 to D24 in [decisions.md](decisions.md) were made after this interview and replace the matching parts of this document. D24 covers all 20 Italian regions and replaces the Tuscany-only first version of D22. Weather is prepared by a scheduled background run and stored, as D15 now states. Weather cells are 0.1 degrees. Only squares with at least 10 percent forest get estimates. D21 authorizes 30 days of weather history, replacing D18's 21-day limit. Method v4 uses reconstructed past weather for completed days and forecasts for today and the next seven days. Habitat and current conditions stay internal to one public score. The zoomed-out map shows only forests. Estimates are stored compactly and only the current and previous assessment dates are kept.

| Interview decision | Agreed requirement |
|---|---|
| Meaning of the percentage | The estimated chance that porcini are growing in the selected area on the selected day. |
| Geographic coverage | All Italy. D24 covers all 20 Italian regions, after D22 had limited the launch to Tuscany. Further countries have no launch commitment. |
| Dates | Today and the following seven days, giving eight selectable dates. |
| Languages | Italian and English. |
| Access and funding | Free, without advertising or subscriptions. |
| Selected features | The 16 MVP features listed below. All other catalogued features belong in one later epic. |
| Local detail | Guide people to parts of a forest a few hundred metres across. |
| Evidence required at launch | Experimental estimates are acceptable when labelled as unvalidated. No owner-led field validation. |
| Improvement over time | Refresh weather automatically in the MVP. Automatic improvement of the prediction method is later work. |
| Running costs | Prefer no new recurring costs. Up to EUR 10 a month is the planning ceiling if needed. Reuse the completed P6 costs and allowance assessment. |

The cost preference does not select a supplier or authorize a purchase. The repository rule against buying a domain, server, API or subscription remains in force.

The owner will evaluate the complete MVP locally before considering a domain or VPS. Development, scheduled jobs and acceptance checks must run on the local machine. Phones can access that machine on the local network for browser checks. The MVP plan must not require a public server, DNS changes, paid services or deployment credentials. Public deployment is separate work after the owner confirms satisfaction with the complete product.

## Main user journey

1. A visitor opens the map in Italian or English without registering.
2. They search for a place, choose to use their location, or move the map.
3. They view experimental probabilities for today or one of the next seven days.
4. They select an area to inspect its percentage, weather, contributing factors and data limitations.
5. They save the area as a private favorite or open its destination in an external navigation app.
6. On a later visit in the same browser, they reopen a favorite and see the current estimates.

The website must support this complete journey on both a phone and a desktop. Location permission is optional. Search and manual map browsing remain available if the visitor declines it.

## MVP feature scope

Feature IDs match the [competitor and feature comparison](../reports/Porcini%20app%20feature%20comparison.md). That report documents 27 products and their public claims. Competitor claims are examples, not evidence that their predictions work.

| ID | Required behavior in the MVP |
|---|---|
| F01 | Show an experimental percentage for the chance that porcini are growing in the selected map area on the selected day. |
| F02 | Support all Italy, including Sicily and Sardinia. Under D24 the map serves all 20 regions. Show missing data explicitly wherever an estimate cannot be produced. |
| F04 | Show estimates for today. |
| F05 | Show estimates for each of the next seven days. |
| F11 | Explain the weather and habitat facts that contributed to the displayed estimate. |
| F12 | Show data sources, update times, missing information, uncertainty and the real level of detail of the inputs. |
| F13 | Provide place search, optional current location and selection of an area on the map. |
| F14 | Offer at least two map background styles, layer visibility controls and adjustable probability-layer opacity. |
| F18 | Show weather for the selected area and day, plus recent rain over selectable periods. Include temperature and available information about drying, frost or hail. |
| F23 | Save private favorite points or map areas with editable names and optional colors in a favorites list. |
| F39 | Open the selected destination in an external navigation app. |
| F70 | Make every MVP capability free, without advertising or subscriptions. |
| F72 | Provide a responsive website with layouts suited to phones and desktops. |
| F75 | Translate the complete user journey, including errors and explanations, into Italian and English. |
| F76 | Allow every MVP capability without registration. |
| F81 | Deliver a coherent, carefully designed interface. Evaluate two mockups before production UI implementation. |

### Percentage meaning and calculation

The event being estimated is porcini growing somewhere within the selected map area on that day. This concerns the mushrooms visible above ground. It does not measure the percentage of land covered by mushrooms, the share of weather rules met, or a particular visitor's chance of finding them.

Use the existing 500 m grid as the starting implementation choice for the requested local detail. It provides equal-sized areas and reuses the existing data joins. The owner chose detail of a few hundred metres, not evidence of prediction accuracy at 500 m. Coarser weather data must remain visible in the explanation.

Treat porcini as one group in the MVP. Reuse the existing group of Boletus edulis, B. aereus, B. reticulatus and B. pinophilus. A species selector belongs to F08 in the roadmap.

The initial method must use real weather and habitat inputs. Start with the published knowledge and implementation work recorded in Epic 1. Document the calculation, its assumptions and how it produces the displayed percentage. Check whether the pilot's season and altitude assumptions fit national use before carrying them into the method. A rule count must not silently become a measured probability. The method remains an experimental estimate even when its software works correctly.

Display whole percentages with an experimental label. Explain the unvalidated status in the map legend and selected-area details. Do not invent accuracy percentages, numerical uncertainty ranges or confidence labels. In particular, do not reuse the made-up scores or reliability labels from the P5 demonstration.

The MVP needs software and data checks for invalid inputs, missing values, date handling and repeatable calculations. It does not need a new field study, a measured accuracy threshold or proof that displayed percentages match observed frequencies before launch.

### Coverage, dates and updates

Use the same calculation and input requirements across Italy. National coverage includes the Alps, Apennines, islands and other Italian regions. The Tuscany-only forest inventory cannot be the only habitat source. Reuse the broader terrain and forest sources investigated in P2, with regional detail where it exists and its age disclosed. Under D22 the first version prepares and publishes estimates for Tuscany only. Outside Tuscany the map and area details say that estimates cover Tuscany for now and show no percentage.

Use Italian local days in Europe/Rome. Language selection must not change which dates a forecast represents. The date control includes today and days one through seven ahead. Each selected area shows its target date and the time its estimate was produced.

Prepare weather ahead on a schedule, as decided in D15. A nightly background run fetches the weather of every weather cell that holds forest squares and stores it, and visitors read the stored result. The window is 38 days: the 30 completed Italian days before today, today and the next seven days, under D21. Store each completed day once and reuse it without a time limit. Do not treat a forecast as a completed day when its date passes. Fetch the newly completed day from the historical source and store it as history. A failed request must not become a stored gap, and an explicit correction creates a new history version while the older rows stay. The national goal stays. The current code runs the schedule for Tuscany only.

Show a completed update consistently across the map and area details. A failed fetch must not erase an area's last stored estimates. Mark older results with their actual date and update time. Never relabel yesterday's estimate as today's estimate or present a past run's seventh day as a forecast for a later date.

When required inputs are missing, show that no estimate is available and explain why. Missing data is not a zero percent prediction. A lack of past sightings is not evidence that porcini cannot grow there.

National coverage requires an input-coverage check across the country. Genuine source gaps can be visible exceptions. They must not conceal a release that only calculates estimates for the original pilot area.

### Explanations and weather

The selected-area details must connect the percentage to the actual inputs used by that calculation. Explain relevant recent rain, temperature and habitat factors in plain language. Avoid generic explanations that remain unchanged when the inputs change.

Provide recent rain totals for 7, 14 and 21 completed days as the initial period choices. Keep those totals separate from predicted rain. Show the selected day's weather with its date. Identify modelled weather as such rather than presenting it as a measurement at the clicked point.

Show temperature and any supported drying, frost or hail information from the selected sources. Missing optional weather information must not turn into an invented value. Acquiring a separate hail or weather-station service is not required by this row.

Sources and freshness must be easy to find. Explain that weather can cover several kilometres even when the map draws 500 m areas. More days ahead must not imply greater certainty. Data completeness and forecast duration are factual properties, not measured prediction accuracy.

Terrain and forest facts can explain a prediction in the MVP. Separate tools for exploring terrain, forest and soil layers remain F15, F16 and F17 in the roadmap.

### Map interaction

Search must locate Italian places and let the visitor select a result. Current location requires a deliberate action and browser permission. Selecting a map area must give consistent coordinates, date, percentage and detail information.

F14 includes switching between at least two readable background styles based on OpenStreetMap data. It also includes hiding and showing the probability overlay and adjusting its opacity. The mockup work will decide how these controls fit the interface. This requirement does not add satellite imagery or the deferred environmental layers.

Use the providers and restrictions already assessed in P6. Do not assume that permission to use OpenStreetMap data also grants unlimited use of the standard openstreetmap.org map server.

The map shows mushroom scores at every zoom, as revised in D19. Wide views average stored forest-square scores into larger areas, using the same date and color scale as local views. Areas with missing scores are marked as missing. Selecting an overview area zooms into its individual 500 m squares. Overview reads do not request new weather. Panning, zooming, changing dates and changing backgrounds must not leave mismatched results on the screen.

### Favorites and external navigation

Store favorites in the current browser without an account. Support adding a selected point or grid area, assigning a name and optional color, reopening it, renaming it and removing it. A saved grid area does not require the forest-boundary drawing tool in F22.

Explain that favorites remain in this browser and can be lost when browser data is cleared. They do not synchronize between devices. Favorites must not create public sightings or feed prediction improvement. Removing a favorite is MVP behavior even though broader record import, export and account deletion belong to later features.

The external navigation action must use the destination the visitor selected. It hands that destination to another app. Route planning, parking discovery, GPS recording and return-to-car guidance remain roadmap features.

### Language and interface quality

The initial language can follow the browser, with an obvious Italian and English switch. Preserve the selected area and date when changing language. Translate controls, explanations, notices, empty states and errors. Keep dates, units and decimal formatting consistent with the selected language.

The map, legend and selected-area details must be readable on a phone. Controls must remain usable while a details panel is open. Desktop layouts must use the available space for the same complete journey.

Use consistent typography, spacing, colors and component behavior. Provide clear loading, empty, unavailable and error states. Date and layer changes must acknowledge the action while data loads. Do not rely on color alone to explain an estimate or missing data. Support keyboard access to controls and readable text alternatives for the selected area's result.

The final UI must implement the design selected through the mockup epic. Visual review on phone and desktop is part of acceptance, alongside functional checks.

## Mockup epic requirements

The MVP scope is now known. Create a dedicated MVP epic for comparing two design alternatives before production UI implementation.

The epic must deliver a Google Stitch prompt based on this PRD and a mockup produced with that prompt. It must also deliver a distinct mockup created directly by the agent. Both alternatives must cover phone and desktop layouts for the same MVP journey, so the owner can compare them fairly.

The brief must include the map, eight-date control, experimental percentage, area explanations, weather, search, background and opacity controls, favorites, navigation and language switching. It must show loading, missing-data and error states. It must include the curated visual quality required by F81.

The owner evaluates both alternatives and chooses a direction before production UI work begins. Record the chosen layout, visual rules and any requested changes. The mockups must not introduce accounts, journals, alerts or other roadmap features into the MVP.

The epic owns the detailed prompt and the mockups. This PRD supplies their scope. No visual direction has been chosen during the interview.

## Technical starting point

Use [Book Corners](https://github.com/andreagrandi/book-corners) as the implementation reference. Reuse its established structure and tools where they meet these requirements.

| Part | Starting choice and reason |
|---|---|
| Server and API | Python, Django and Django Ninja, following the Book Corners application and endpoint patterns. |
| Geographic storage | PostgreSQL with PostGIS, following the existing geographic database setup. |
| Pages and interaction | Django templates and HTMX. Keep map interactions in the map component. |
| Styling | Tailwind CSS and daisyUI as implementation tools for the selected design. Their default appearance does not satisfy F81 by itself. |
| Map rendering | Start from Book Corners' Leaflet approach. Use the P5 work as evidence about serving cells and phone limits. Document a concrete rendering need if another map library is required. |
| Background work | Reuse the Django background-task and scheduled-job patterns for habitat builds and the nightly cleanup of old weather and estimates. |
| Languages | Reuse Django's translation patterns from Book Corners. |
| Development and checks | Reuse Docker Compose, application process and GitHub Actions check patterns. Keep SporaMeteo configuration separate. Do not copy automatic deployment jobs or require VPS credentials. |

Reuse useful SporaMeteo data processing code and the established grid and date conventions. P5's demonstration API and page are experiments, not a requirement to retain their production architecture. Book Corners' account dependencies are unnecessary while F77 is deferred.

Prepare weather ahead in a scheduled run, store the results and serve the stored results to every visitor. Map browsing must not trigger one Open-Meteo request per visitor or per 500 m square. Nearby squares can share the weather that actually covers them while retaining their separate habitat inputs.

Keep the method version, input sources, source dates and calculation time with each published update. This supports explanations and later changes to the method without requiring an automatic learning system in the MVP.

## Existing costs and provider findings

Use the completed [P6 assessment](../reports/p6_cost_licence_portability.md) and [licence table](../reports/p6_licences.csv). This work is not an unresolved product question and must not become another general provider-cost investigation.

P6 checked the following on 11 October 2026.

| Finding | How the MVP uses it |
|---|---|
| The free Open-Meteo plan permits 600 calls a minute, 5,000 an hour, 10,000 a day and 300,000 a month. | Count usage according to the provider's rules and keep scheduled work within all limits. |
| A free app without ads or subscriptions fits the non-commercial example in the recorded terms. | Use the free service under the selected business model. |
| Requests with more than 10 variables or more than 14 days can count as several calls. | Include counted usage rather than HTTP request totals alone. |
| The Forecast API can supply recent weather and the upcoming forecast. Paid historical APIs were not required for the proposed live app. | Reuse this source choice for production weather. |
| The assessed 28,205 square kilometre home area used 392 weather cells and an estimated 1,036 counted calls a day. | Reuse the shared-cell and caching approach. Check the count for the selected national coverage during implementation. |
| Weather fetching does not grow with the number of visitors. | Calculate updates once and share stored results. |
| P6 recorded available hosting with backups at EUR 7.20 to EUR 9.74 a month, depending on contract length. | Use the existing assessment for the EUR 10 planning ceiling. These are dated findings, not a purchase decision. |
| OpenFreeMap was free without a view limit. P6 also identified a self-hosted Protomaps file as an alternative. | Start with these assessed background-map options. Check phone behavior with the selected implementation. |

The recorded allowance is established. Counting the national weather cells and actual update requests is an implementation check, because the measured P6 workload covered the home area. It does not reopen the pricing research or change the agreed national scope.

Carry forward the data conditions already identified in P6. Replace GADM outlines before distribution. Retain required source credits. Respect the sharing conditions for any published material derived from the Tuscany forest inventory. Do not publish research finds or their photos as an unrequested app feature.

## Local MVP acceptance

The implementation tickets must turn these requirements into concrete checks against the locally running product. Completing automated checks does not replace the owner's final acceptance.

| Area | Required evidence before local MVP acceptance |
|---|---|
| Complete journey | A visitor can search, select an area and date, read its estimate and explanation, save and reopen a favorite, and open navigation on phone and desktop. |
| Coverage | Under D24 the published runs cover every forest square of the 20 Italian regions, one run per region. A place with no published estimate shows a stated reason and no percentage. #156 checked the estimate method at 10 places outside Tuscany. |
| Dates | All eight dates use Europe/Rome correctly, including date rollover and daylight-saving changes. Weather and percentages refer to the selected date. |
| Calculation | Real inputs produce repeatable estimates. Invalid or missing inputs cannot create made-up percentages. The calculation and method version are recorded. |
| Experimental status | The legend and area details explain that probability accuracy has not been validated. No screen claims validated accuracy or guaranteed finds. |
| Updates | A scheduled run fetches and stores the 38-day weather window ahead of visitors, and requests read the stored results. Failed fetches keep correctly dated previous results and show their age. |
| Favorites | Names, colors and locations persist across a reload in the same browser. Rename and removal work. No account or public submission is created. |
| Map behavior | Repeated panning, zooming, date changes and background changes remain usable on an actual iPhone with Safari and an Android phone browser, plus desktop browsers. |
| Known phone failure | Repeat a sustained map interaction comparable to P5, including at least 25 moves. The production page must not repeat the progressive slowdown and freeze reported there. |
| UI quality | The owner has evaluated both mockups. The implemented phone and desktop layouts follow the selected design and include loading, empty, missing-data and error states. |
| Languages | The complete journey works in Italian and English, including failures and explanations. |
| Service use | Recorded update usage fits the established Open-Meteo allowance. Browsing does not trigger repeated weather downloads. Attribution appears where required. |
| Owner acceptance | The owner has tried the complete local MVP and explicitly confirmed satisfaction. Record that decision before any later public deployment or hosting investment. |

No item requires the owner to collect mushroom observations. The release has no field-accuracy target and no automatic model-learning requirement. The experimental label does not excuse broken calculations, fabricated inputs or an unfinished interface.

## Epics and tickets after this PRD

The [published MVP issue index](mvp_github_issues.md) links the full MVP epics, bounded sub-tickets and acceptance criteria created from this PRD. They cover the application foundation, Italian habitat coverage, scheduled weather updates, experimental estimates, the mockup comparison, the selected UI, favorites and navigation, and local acceptance checks.

The final MVP epic ends with acceptance of the complete local product. No MVP ticket purchases or publishes to a VPS, buys a domain, changes DNS or enables automatic deployment. Hosting and public launch are deferred until the owner accepts the product and authorizes that later work.

The mockup comparison must be an explicit MVP epic. Its selected result is a dependency of production UI work. Background data and application work can proceed without selecting a visual design first.

All deferred work goes into one epic named exactly "Features Roadmap". It must have no sub-tickets. Use the grouped feature tables below as its description. They record possibilities for later selection, not a commitment to build every feature or a delivery order.

## Features Roadmap

### Growth estimates and dates

| ID | Feature |
|---|---|
| F03 | A separate habitat map showing where porcini can live, apart from daily growth estimates. |
| F06 | Replay past dates and distinguish saved predictions from later reconstructions. |
| F07 | Compare areas, rank nearby options and suggest the best day for an area. |
| F08 | Distinguish individual porcini types or species. |
| F09 | Add other mushrooms or edible plants. |
| F10 | Show seasonal calendars and stages of mushroom growth. |
| F82 | Automatically improve the prediction method using new public mushroom observations. |

### Map controls and environmental information

| ID | Feature |
|---|---|
| F15 | Explore elevation, slope, direction, shade and drainage. |
| F16 | Explore forest type, host trees, tree age, density and cover. |
| F17 | Explore soil acidity, moisture and temperature. |
| F19 | Show weather stations, historical charts and downloadable reports. |
| F20 | Map protected areas, gathering restrictions and closed days. |
| F21 | Show published sightings with species, date and identification filters. |
| F22 | Draw or select forest boundaries for analysis. |

### Personal places and outing records

| ID | Feature |
|---|---|
| F24 | Keep a find journal with dates, locations, species, quantities and condition details. |
| F25 | Record unsuccessful searches, time spent and possible reasons for finding nothing. |
| F26 | Fill records with weather and photo dates or locations, with corrections. |
| F27 | Attach photos, text and voice notes, with bulk import. |
| F28 | Compare outings with weather and predictions, and summarize seasons and previous years. |
| F29 | Reconstruct past outings from records or photos entered later. |
| F30 | Offer account synchronization and storage choices for personal records. |
| F31 | Back up, restore, import, export and delete broader personal records. |
| F32 | Monitor places or species and send weather, growth or reminder alerts. |

### Offline use

| ID | Feature |
|---|---|
| F33 | Download map backgrounds and terrain or forest layers. |
| F34 | Keep forecasts and area information available offline with their date and expiry. |
| F35 | Read and create personal records offline, with later synchronization. |
| F36 | Use identification and reference tools offline. |
| F37 | Use downloaded routes and return guidance without a connection. |
| F38 | Manage download sizes, interrupted downloads, repairs and removal. |

### Reaching an area and recording the walk

| ID | Feature |
|---|---|
| F40 | Show trails, contours, 3D terrain and printable maps. |
| F41 | Create walking routes, estimate travel time and plan multi-day trips. |
| F42 | Record GPS routes, duration and distance. |
| F43 | Save parking and provide compass, altitude and return-to-car guidance. |
| F44 | Share a live outing, last position and expected return deliberately. |
| F45 | Provide wrong-turn and group-separation alerts, return checks and emergency messages. |
| F46 | Import and export route files and exchange routes with watches. |
| F47 | Record finds through lock-screen or watch controls. |

### Privacy, sharing and community

| ID | Feature |
|---|---|
| F48 | Control exact, approximate, trusted-person and private visibility for records. |
| F49 | Share selected places or records by link, QR code or file, with optional expiry. |
| F50 | Let users decide whether their personal records may improve predictions. |
| F51 | Publish and search community observations or approximate totals. |
| F52 | Offer invitations, groups, companions, chat, posts and followed activity. |
| F53 | Provide reporting, blocking, moderation and appeals. |
| F54 | Ask the community to suggest or review mushroom identifications. |
| F55 | Show verified experts or trusted contributors. |
| F56 | Add achievements, friend challenges, rankings and rubbish-collection records. |
| F57 | Export account data and delete accounts. |

### Identification and learning

| ID | Feature |
|---|---|
| F58 | Provide species reference cards with names, photos, habitat and season. |
| F59 | Compare similar species and explain dangerous lookalikes. |
| F60 | Suggest species from photos, with clear limits on identification. |
| F61 | Guide identification through visible traits and illustrated questions. |
| F62 | Offer consultation with a human expert. |
| F63 | Provide quizzes, practice exams and review of mistakes. |
| F64 | Provide field exercises and learning progress. |
| F65 | Publish guides, growth bulletins, newsletters and website badges. |

### Permits and commercial services

| ID | Feature |
|---|---|
| F66 | Sell permits and retain purchase history or wallet passes. |
| F67 | Store an existing permit's authority, number, receipt, cost and expiry. |
| F68 | Notify users about restrictions and changes to gathering rules. |
| F69 | Offer outdoor bookings, authority or retailer tools and member benefits. |
| F71 | Suggest recipes and routes between ingredient locations. |

### Website access and supporting choices

| ID | Feature |
|---|---|
| F73 | Provide native phone or tablet apps. |
| F74 | Make the website installable as an app. |
| F77 | Provide accounts and sign-in methods. |
| F78 | Show other outdoor conditions, such as snow, air quality or insect activity. |
| F79 | Accept local weather corrections and structured field measurements. |
| F80 | Publish prediction evaluation methods, errors and failed targets. |

## Source documents and evidence limits

The analysis followed the Markdown links from the planning handoff recursively. The following documents informed this PRD.

| Document | Contribution |
|---|---|
| [Planning after Epic 1](planning_after_epic1.md) | Entry point to the completed investigation. |
| [Epic 1 validation report](../reports/EPIC1_VALIDATION_REPORT.md) | Combined findings and the distinction between tested capabilities and proposed next work. |
| [Decisions](decisions.md) | Existing grid, time, species, website and map choices. The product interview supersedes the earlier scope and validation gate. |
| [Epic 1 PRD](../SporaMeteo_PRD_Epic_1_Validation_EN.md) | Investigation scope and constraints. It remains the authority for that completed epic. |
| [Data formats](data_formats.md) | Grid identifiers, dates and reusable data conventions. |
| [Test rules](test_rules.md) | How the investigation separated years and areas and judged rankings. Those tests did not establish calibrated growth probabilities. |
| [P6 costs and licences](../reports/p6_cost_licence_portability.md) | Assessed free services, allowances, costs, licences and distribution conditions. |
| [P5 phone map](../reports/p5_phone_map.md) | Efficient result serving and the unresolved iPhone freeze in the demonstration. |
| [API README](../apps/api/README.md) | Demonstration API behavior and its synthetic data. |
| [P4 benchmark](../reports/p4_benchmark.md) | Prediction experiments, published rules and limits of the observed results. |
| [Context dossier](../SporaMeteo_Context_Dossier_EN.md) | Background, not authority for MVP feature selection. |
| [P1 observations](../reports/p1_observations.md) | Available records, location and date quality, and the shortage of reliable local outcomes. |
| [P1c season signs](../reports/p1c_flush_signs.md) | Indirect season evidence that cannot replace growth observations. |
| [P2 habitat](../reports/p2_habitat.md) | Terrain and forest data, old regional tree records and uncertain soil information. |
| [P3 weather](../reports/p3_weather.md) | Recent and future weather access, real weather detail, caching and dates of available forecasts. |

The [P6 licence table](../reports/p6_licences.csv) and [P4 settings](../experiments/p4b_benchmark/config.toml) provide supporting detail. The [feature comparison](../reports/Porcini%20app%20feature%20comparison.md) links the competitor sources and detailed research notes.

Epic 1 did not validate fine-scale growth probabilities. Some weather evidence covers 9 km or 25 km areas. The habitat pilot covered only 504 squares, and regional tree records can be old. These limits inform the explanations and implementation work. They do not replace the owner's chosen companion product with a different MVP.
