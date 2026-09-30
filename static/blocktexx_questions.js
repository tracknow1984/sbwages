window.BlocktexxQuestions=(()=>{
 const bank=[
  {
    "id": "q001",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Which services must the quoted cost per kg include: collections, bin hire, weighing, decommissioning transfers, baling, interstate freight, shredding transfers and final delivery?",
    "priority": true
  },
  {
    "id": "q002",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Can storage and repacking be contracted and invoiced separately from the transport rate per kg?",
    "priority": true
  },
  {
    "id": "q003",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Who pays the decommissioning and shredding fees, and should these be excluded from SB Empire’s transport price?",
    "priority": true
  },
  {
    "id": "q004",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Which states and territories are in the initial scope, and is Western Australia included from commencement?",
    "priority": false
  },
  {
    "id": "q005",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Who is responsible for each handover, and when does custody and responsibility for the stock transfer?",
    "priority": false
  },
  {
    "id": "q006",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Who can authorise extra services, changes to scope and additional charges?",
    "priority": false
  },
  {
    "id": "q007",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "Will Blocktexx provide a single national contact and operational decision-maker for each state?",
    "priority": false
  },
  {
    "id": "q008",
    "group": "Scope and accountability",
    "scope": "National",
    "question": "What are the required start date, rollout stages and contract term?",
    "priority": false
  },
  {
    "id": "q009",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "What minimum monthly kilograms will Blocktexx commit to in each state, and what happens commercially if actual volumes fall below that commitment?",
    "priority": true
  },
  {
    "id": "q010",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "Is the chargeable weight measured at first collection, after decommissioning, after shredding or on accepted delivery to Blocktexx?",
    "priority": true
  },
  {
    "id": "q011",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "Can we obtain at least 12 months of pickup-level weights, dates, locations and equipment quantities, reconciled to monthly totals?",
    "priority": true
  },
  {
    "id": "q012",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "Are bin, cage, pallet, packaging and moisture weights excluded, and what tare method should be used?",
    "priority": false
  },
  {
    "id": "q013",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "How are contamination, rejected stock, processing loss and differences between weigh points treated for billing?",
    "priority": false
  },
  {
    "id": "q014",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "What are the expected average, peak and low monthly volumes by state and customer, including seasonal changes?",
    "priority": false
  },
  {
    "id": "q015",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "What volume growth is expected, and how much notice will be provided for new customers or major increases?",
    "priority": false
  },
  {
    "id": "q016",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "Can current sample locations, unmatched kilograms and customers without calendar allocations be confirmed?",
    "priority": false
  },
  {
    "id": "q017",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "Are additional movements of the same stock billed separately rather than counting its kilograms again?",
    "priority": false
  },
  {
    "id": "q018",
    "group": "Volumes and the chargeable kilogram",
    "scope": "National",
    "question": "What material categories and average bulk densities should we assume for vehicle and storage capacity?",
    "priority": false
  },
  {
    "id": "q019",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Can Blocktexx confirm every pickup address, contact, opening hours and required collection frequency?",
    "priority": true
  },
  {
    "id": "q020",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Which pickup days or time windows are fixed, and which can be changed to improve route efficiency?",
    "priority": true
  },
  {
    "id": "q021",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "What are the expected kg and bins per pickup, and are minimum pickup quantities acceptable?",
    "priority": true
  },
  {
    "id": "q022",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Is every pickup a like-for-like empty/full bin exchange, including the same sizes and quantities?",
    "priority": false
  },
  {
    "id": "q023",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Who provides loading equipment and labour at each customer, and where is a tail lift required?",
    "priority": false
  },
  {
    "id": "q024",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Which locations have vehicle size, loading dock, height, parking or access restrictions?",
    "priority": false
  },
  {
    "id": "q025",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "What waiting allowance is acceptable, and can additional waiting or failed access be charged?",
    "priority": false
  },
  {
    "id": "q026",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "How much notice is required for extra, cancelled, missed, emergency or after-hours pickups?",
    "priority": false
  },
  {
    "id": "q027",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "Who pays for unsuccessful collections, empty bins, underfilled bins or stock that is not ready?",
    "priority": false
  },
  {
    "id": "q028",
    "group": "Customer collections and access",
    "scope": "National",
    "question": "What pickup evidence is required: signature, photos, scanned bin IDs, weights and time stamps?",
    "priority": false
  },
  {
    "id": "q029",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "Can Blocktexx confirm the decomm partner, address and contact for every state, including backup partners?",
    "priority": true
  },
  {
    "id": "q030",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What kilograms can each partner accept per delivery and per week, and what is its maximum stock holding capacity?",
    "priority": true
  },
  {
    "id": "q031",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What kg per day or week can each partner decommission, and what is the normal and maximum turnaround?",
    "priority": true
  },
  {
    "id": "q032",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What delivery and collection frequencies and preferred days should we plan for each partner?",
    "priority": false
  },
  {
    "id": "q033",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "Who pays the partner’s processing, handling, storage and other charges?",
    "priority": false
  },
  {
    "id": "q034",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "How will we be notified when decommissioned stock is ready for collection?",
    "priority": false
  },
  {
    "id": "q035",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What packaging or containment is required on delivery and after decommissioning?",
    "priority": false
  },
  {
    "id": "q036",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What contamination or non-textile items must be removed, and who handles rejects and waste?",
    "priority": false
  },
  {
    "id": "q037",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What happens to stock and costs if a partner is full, delayed, closed or unable to meet quality requirements?",
    "priority": false
  },
  {
    "id": "q038",
    "group": "Decommissioning partners",
    "scope": "National",
    "question": "What completion and quality evidence is required before stock can be baled and dispatched?",
    "priority": false
  },
  {
    "id": "q039",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "What bale dimensions, target weight, density, strapping and wrapping specifications will Threadtexx accept?",
    "priority": true
  },
  {
    "id": "q040",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "Who supplies and pays for balers, forklifts, weighing equipment, consumables, maintenance and breakdown cover?",
    "priority": true
  },
  {
    "id": "q041",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "What baling throughput and labour hours should be allowed per tonne, and who is responsible at partner depots?",
    "priority": true
  },
  {
    "id": "q042",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "What depot floor area, holding capacity and handling equipment are required in each state?",
    "priority": false
  },
  {
    "id": "q043",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "What weighbridge or scale accuracy, calibration and weigh-docket evidence are required?",
    "priority": false
  },
  {
    "id": "q044",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "What batch identification, source segregation and traceability must be maintained through baling?",
    "priority": false
  },
  {
    "id": "q045",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "Who pays for unloading, reloading, double handling, pallet replacement and damaged packaging?",
    "priority": false
  },
  {
    "id": "q046",
    "group": "Depots, baling and equipment",
    "scope": "National",
    "question": "Are palletised loads required, can bales be stacked, and what loading pattern and restraints are approved?",
    "priority": false
  },
  {
    "id": "q047",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Can the required bin, cage and pallecon quantities and sizes be confirmed for every customer?",
    "priority": true
  },
  {
    "id": "q048",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Who owns the equipment, and which items are purchased, leased or supplied by Blocktexx?",
    "priority": true
  },
  {
    "id": "q049",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Will rental be charged separately from transport or included in the per-kg price?",
    "priority": true
  },
  {
    "id": "q050",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Is a full second set for switch-outs enough, or is additional stock required while material is at decomm partners?",
    "priority": false
  },
  {
    "id": "q051",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Who pays for cleaning, repairs, theft, loss, damage and end-of-contract equipment recovery?",
    "priority": false
  },
  {
    "id": "q052",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "What spare stock is required, and who bears rental during idle periods or customer pauses?",
    "priority": false
  },
  {
    "id": "q053",
    "group": "Bins, cages and resource rental",
    "scope": "National",
    "question": "Are delivery, relocation and collection of empty equipment separately chargeable?",
    "priority": false
  },
  {
    "id": "q054",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "What exact dispatch threshold applies at each VIC, SA and WA partner depot: kilograms, bales, pallet spaces or a combination?",
    "priority": true
  },
  {
    "id": "q055",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "Is all VIC, SA and WA stock to travel through Sydney, and when may a direct-to-QLD route be approved?",
    "priority": true
  },
  {
    "id": "q056",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "What constitutes a full B-double: target payload, usable pallet spaces, bale capacity and loading constraints?",
    "priority": true
  },
  {
    "id": "q057",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "What maximum holding period is allowed if a depot has not reached its dispatch threshold?",
    "priority": false
  },
  {
    "id": "q058",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "Who pays for a part-load or urgent dispatch requested before the threshold is reached?",
    "priority": false
  },
  {
    "id": "q059",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "What Sydney depot capacity and loading windows are available for consolidation and northbound dispatch?",
    "priority": false
  },
  {
    "id": "q060",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "Who books interstate transport and pays tolls, fuel levies, demurrage and depot handling?",
    "priority": false
  },
  {
    "id": "q061",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "How should fuel-levy changes and carrier price increases be passed through during the contract?",
    "priority": false
  },
  {
    "id": "q062",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "Are empty-equipment returns or backloads required, and who pays for them?",
    "priority": false
  },
  {
    "id": "q063",
    "group": "Interstate consolidation and dispatch",
    "scope": "National",
    "question": "What transit times, delivery appointments, proof of delivery and delay notifications are required?",
    "priority": false
  },
  {
    "id": "q064",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "What intake capacity, booked delivery slots and stock holding limits does Threadtexx have?",
    "priority": true
  },
  {
    "id": "q065",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "What shredding throughput and turnaround should be used to plan collections of finished material?",
    "priority": true
  },
  {
    "id": "q066",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "Who decides whether shredded stock goes directly to Blocktexx Loganholme or into North Maclean storage?",
    "priority": true
  },
  {
    "id": "q067",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "What packaging, weights and vehicle requirements apply to shredded material?",
    "priority": false
  },
  {
    "id": "q068",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "How much notice is provided for releases from storage to production, and what minimum load is acceptable?",
    "priority": false
  },
  {
    "id": "q069",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "What hours, unloading resources and appointment rules apply at Blocktexx Loganholme?",
    "priority": false
  },
  {
    "id": "q070",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "Who pays for production delays, rejected deliveries, extra handling or returns from Threadtexx or Blocktexx?",
    "priority": false
  },
  {
    "id": "q071",
    "group": "Threadtexx and production movements",
    "scope": "National",
    "question": "How are process losses, output weight differences and batch reconciliations reported?",
    "priority": false
  },
  {
    "id": "q072",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Can the retained container commitment, monthly rate, free-container allowance and contract duration be confirmed?",
    "priority": true
  },
  {
    "id": "q073",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Can the current full and empty container counts and NSW, BANYO and BAGS allocations be verified?",
    "priority": true
  },
  {
    "id": "q074",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Can current pallet ranges and achievable after-repack capacity be confirmed with a trial?",
    "priority": true
  },
  {
    "id": "q075",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Who pays the one-off repacking cost, and what repacking sequence and stock access constraints apply?",
    "priority": false
  },
  {
    "id": "q076",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Is 5–6 source containers repacked per week acceptable, and what staging space can be made available?",
    "priority": false
  },
  {
    "id": "q077",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "How will freed capacity, incoming stock and production releases be allocated between the storage sections?",
    "priority": false
  },
  {
    "id": "q078",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "What maximum storage duration, stock rotation, security and inspection standards are required?",
    "priority": false
  },
  {
    "id": "q079",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "Who pays for stocktake, stock retrieval, handling, damaged stock and movements in or out of storage?",
    "priority": false
  },
  {
    "id": "q080",
    "group": "Storage and repacking — separate scope",
    "scope": "National",
    "question": "What happens if storage demand exceeds contracted capacity, or if Blocktexx seeks to reduce the commitment?",
    "priority": false
  },
  {
    "id": "q081",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "Does Blocktexx require a single national cost per kg, or can each state have its own rate reflecting volume and resources?",
    "priority": true
  },
  {
    "id": "q082",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "Will Blocktexx accept a minimum monthly charge or capacity reservation to cover committed trucks, depots and labour?",
    "priority": true
  },
  {
    "id": "q083",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "What payment terms, billing frequency, purchase orders and supporting records are required?",
    "priority": true
  },
  {
    "id": "q084",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "Which costs can be separately charged: rentals, storage, repacking, waiting, urgent work and extra movements?",
    "priority": false
  },
  {
    "id": "q085",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "What review mechanism applies when volumes, routes, collection frequencies or the state mix change?",
    "priority": false
  },
  {
    "id": "q086",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "How will wage, fuel, insurance, rent and equipment cost changes be handled over the contract term?",
    "priority": false
  },
  {
    "id": "q087",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "Are forecast volumes binding, and is there compensation for early termination or unrecovered setup commitments?",
    "priority": false
  },
  {
    "id": "q088",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "What dispute process and timeframe apply to weights, service performance and invoices?",
    "priority": false
  },
  {
    "id": "q089",
    "group": "Pricing, invoicing and changes",
    "scope": "National",
    "question": "Can the agreed quote clearly distinguish GST, inclusions, exclusions and provisional assumptions?",
    "priority": false
  },
  {
    "id": "q090",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What service levels are required for pickup reliability, turnaround and delivery, and how are failures measured?",
    "priority": true
  },
  {
    "id": "q091",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What insurance limits and responsibility for loss, damage, contamination and stored stock are required?",
    "priority": true
  },
  {
    "id": "q092",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What material acceptance rules apply to hazardous, wet, mouldy, contaminated or otherwise unsuitable loads?",
    "priority": true
  },
  {
    "id": "q093",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What supplier onboarding, site induction, training and safety requirements apply to company and contractor crews?",
    "priority": false
  },
  {
    "id": "q094",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What contingency capacity is needed for truck failures, partner outages, weather disruption and peak volumes?",
    "priority": false
  },
  {
    "id": "q095",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "What reporting, data integration, audit trail, sustainability evidence and retention periods are required?",
    "priority": false
  },
  {
    "id": "q096",
    "group": "Service standards and operational risk",
    "scope": "National",
    "question": "Who signs off final assumptions and readiness before the operating plan and price are committed?",
    "priority": false
  },
  {
    "id": "state-qld-1",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "For QLD, can Blocktexx confirm the operating model uses SB Empire depot and company trucks, including any exceptions?",
    "priority": true
  },
  {
    "id": "state-qld-2",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What are the guaranteed, expected and peak monthly kg for QLD, with pickup-level source data?",
    "priority": true
  },
  {
    "id": "state-qld-3",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What depot address, operating hours, vehicle capacity and handling resources are confirmed for QLD?",
    "priority": true
  },
  {
    "id": "state-qld-4",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "Who is the QLD decomm partner, what can it accept/process each week, and what is its turnaround?",
    "priority": true
  },
  {
    "id": "state-qld-5",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What collection and decomm delivery/return frequencies are required for QLD?",
    "priority": false
  },
  {
    "id": "state-qld-6",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "Who returns decommissioned clothing for baling in QLD, and what are the baling cost and capacity?",
    "priority": false
  },
  {
    "id": "state-qld-7",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What bin/cage quantities, switch-out stock and rental arrangements are required for QLD?",
    "priority": false
  },
  {
    "id": "state-qld-8",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What local transfer frequency is required between the QLD depot, Threadtexx, North Maclean and Loganholme?",
    "priority": false
  },
  {
    "id": "state-qld-9",
    "group": "QLD operating checklist",
    "scope": "QLD",
    "question": "What unresolved charges, access limits, local service exceptions or backup arrangements remain for QLD?",
    "priority": false
  },
  {
    "id": "state-nsw-1",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "For NSW, can Blocktexx confirm the operating model uses SB Empire depot and company trucks, including any exceptions?",
    "priority": true
  },
  {
    "id": "state-nsw-2",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What are the guaranteed, expected and peak monthly kg for NSW, with pickup-level source data?",
    "priority": true
  },
  {
    "id": "state-nsw-3",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What depot address, operating hours, vehicle capacity and handling resources are confirmed for NSW?",
    "priority": true
  },
  {
    "id": "state-nsw-4",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "Who is the NSW decomm partner, what can it accept/process each week, and what is its turnaround?",
    "priority": true
  },
  {
    "id": "state-nsw-5",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What collection and decomm delivery/return frequencies are required for NSW?",
    "priority": false
  },
  {
    "id": "state-nsw-6",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "Who returns decommissioned clothing for baling in NSW, and what are the baling cost and capacity?",
    "priority": false
  },
  {
    "id": "state-nsw-7",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What bin/cage quantities, switch-out stock and rental arrangements are required for NSW?",
    "priority": false
  },
  {
    "id": "state-nsw-8",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What full-load threshold and maximum holding time trigger Sydney-to-QLD dispatch?",
    "priority": false
  },
  {
    "id": "state-nsw-9",
    "group": "NSW operating checklist",
    "scope": "NSW",
    "question": "What unresolved charges, access limits, local service exceptions or backup arrangements remain for NSW?",
    "priority": false
  },
  {
    "id": "state-vic-1",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "For VIC, can Blocktexx confirm the operating model uses partner depot and contractor trucks, including any exceptions?",
    "priority": true
  },
  {
    "id": "state-vic-2",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What are the guaranteed, expected and peak monthly kg for VIC, with pickup-level source data?",
    "priority": true
  },
  {
    "id": "state-vic-3",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What depot address, operating hours, vehicle capacity and handling resources are confirmed for VIC?",
    "priority": true
  },
  {
    "id": "state-vic-4",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "Who is the VIC decomm partner, what can it accept/process each week, and what is its turnaround?",
    "priority": true
  },
  {
    "id": "state-vic-5",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What collection and decomm delivery/return frequencies are required for VIC?",
    "priority": false
  },
  {
    "id": "state-vic-6",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "Who returns decommissioned clothing for baling in VIC, and what are the baling cost and capacity?",
    "priority": false
  },
  {
    "id": "state-vic-7",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What bin/cage quantities, switch-out stock and rental arrangements are required for VIC?",
    "priority": false
  },
  {
    "id": "state-vic-8",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What bale/weight threshold and maximum holding time trigger VIC dispatch to Sydney?",
    "priority": false
  },
  {
    "id": "state-vic-9",
    "group": "VIC operating checklist",
    "scope": "VIC",
    "question": "What unresolved charges, access limits, local service exceptions or backup arrangements remain for VIC?",
    "priority": false
  },
  {
    "id": "state-sa-1",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "For SA, can Blocktexx confirm the operating model uses partner depot and contractor trucks, including any exceptions?",
    "priority": true
  },
  {
    "id": "state-sa-2",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What are the guaranteed, expected and peak monthly kg for SA, with pickup-level source data?",
    "priority": true
  },
  {
    "id": "state-sa-3",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What depot address, operating hours, vehicle capacity and handling resources are confirmed for SA?",
    "priority": true
  },
  {
    "id": "state-sa-4",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "Who is the SA decomm partner, what can it accept/process each week, and what is its turnaround?",
    "priority": true
  },
  {
    "id": "state-sa-5",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What collection and decomm delivery/return frequencies are required for SA?",
    "priority": false
  },
  {
    "id": "state-sa-6",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "Who returns decommissioned clothing for baling in SA, and what are the baling cost and capacity?",
    "priority": false
  },
  {
    "id": "state-sa-7",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What bin/cage quantities, switch-out stock and rental arrangements are required for SA?",
    "priority": false
  },
  {
    "id": "state-sa-8",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What bale/weight threshold and maximum holding time trigger SA dispatch to Sydney?",
    "priority": false
  },
  {
    "id": "state-sa-9",
    "group": "SA operating checklist",
    "scope": "SA",
    "question": "What unresolved charges, access limits, local service exceptions or backup arrangements remain for SA?",
    "priority": false
  },
  {
    "id": "state-wa-1",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "For WA, can Blocktexx confirm the operating model uses partner depot and contractor trucks, including any exceptions?",
    "priority": true
  },
  {
    "id": "state-wa-2",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What are the guaranteed, expected and peak monthly kg for WA, with pickup-level source data?",
    "priority": true
  },
  {
    "id": "state-wa-3",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What depot address, operating hours, vehicle capacity and handling resources are confirmed for WA?",
    "priority": true
  },
  {
    "id": "state-wa-4",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "Who is the WA decomm partner, what can it accept/process each week, and what is its turnaround?",
    "priority": true
  },
  {
    "id": "state-wa-5",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What collection and decomm delivery/return frequencies are required for WA?",
    "priority": false
  },
  {
    "id": "state-wa-6",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "Who returns decommissioned clothing for baling in WA, and what are the baling cost and capacity?",
    "priority": false
  },
  {
    "id": "state-wa-7",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What bin/cage quantities, switch-out stock and rental arrangements are required for WA?",
    "priority": false
  },
  {
    "id": "state-wa-8",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What bale/weight threshold and maximum holding time trigger WA dispatch to Sydney?",
    "priority": false
  },
  {
    "id": "state-wa-9",
    "group": "WA operating checklist",
    "scope": "WA",
    "question": "What unresolved charges, access limits, local service exceptions or backup arrangements remain for WA?",
    "priority": false
  }
];
 const e=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
 const answered=a=>a?.status==='not_applicable'||(a?.status==='answered'&&!!a.answer?.trim());
 function render(root,model,onChange,options={}){
  const answers=model.clarification_answers||(model.clarification_answers={}),filters=root._questionFilters||(root._questionFilters={scope:'All',status:'All',search:''});
  root.replaceChildren(e('h2','Questions for Blocktexx'),e('p','Clarifications to confirm scope, resources, service requirements and the cost per kilogram. Record national answers once, then complete the state checklists.'));
  const progress=e('p',null,'bx-question-progress');progress.setAttribute('role','status');root.append(progress);
  const controls=e('div',null,'bx-question-controls'),list=e('div');
  function select(title,key,options){const label=e('label',title),input=e('select');input.setAttribute('aria-label',title);options.forEach(([v,t])=>{const o=e('option',t);o.value=v;input.append(o);});input.value=filters[key];input.onchange=()=>{filters[key]=input.value;draw();};label.append(input);controls.append(label);}
  select('Scope','scope',['All','National','QLD','NSW','VIC','SA','WA'].map(x=>[x,x==='All'?'All states and national':x]));
  select('Show','status',[['All','All questions'],['open','Open / awaiting answers'],['essential','Essential questions'],['answered','Answered / not applicable']]);
  const searchLabel=e('label','Search questions'),search=e('input');search.type='search';search.value=filters.search;search.placeholder='Search volumes, fuel, storage…';search.setAttribute('aria-label','Search questions');search.oninput=()=>{filters.search=search.value;draw();};searchLabel.append(search);controls.append(searchLabel);
  const save=e('button','Save answers','primary');save.type='button';save.onclick=()=>document.getElementById('bx-save')?.click();controls.append(save);
  const expand=e('button','Expand all','secondary');expand.type='button';expand.onclick=()=>list.querySelectorAll('details').forEach(x=>x.open=true);controls.append(expand);
  const collapse=e('button','Collapse all','secondary');collapse.type='button';collapse.onclick=()=>list.querySelectorAll('details').forEach(x=>x.open=false);controls.append(collapse);
  root.append(controls,list);
  const saveStatus=e('p','','bx-muted');saveStatus.setAttribute('role','status');root.append(saveStatus);
  root._questionObserver?.disconnect();const source=document.getElementById('bx-save-status');
  if(source){saveStatus.textContent=source.textContent;root._questionObserver=new MutationObserver(()=>saveStatus.textContent=source.textContent);root._questionObserver.observe(source,{subtree:true,childList:true,characterData:true});}
  function count(){const n=bank.filter(q=>answered(answers[q.id])).length;progress.textContent=n+' of '+bank.length+' answered / not applicable · '+(bank.length-n)+' still to clarify';}
  function draw(){
   list.replaceChildren();count();const term=filters.search.trim().toLowerCase();
   const visible=bank.filter(q=>(filters.scope==='All'||q.scope===filters.scope)&&(filters.status==='All'||filters.status==='essential'&&q.priority||filters.status==='open'&&!answered(answers[q.id])||filters.status==='answered'&&answered(answers[q.id]))&&(!term||(q.question+' '+q.group+' '+(answers[q.id]?.answer||'')).toLowerCase().includes(term)));
   if(!visible.length){list.append(e('p','No questions match these filters.'));return;}
   [...new Set(visible.map(q=>q.group))].forEach((group,index)=>{
    const questions=visible.filter(q=>q.group===group),details=e('details',null,'bx-question-group');details.open=!!term||index===0;details.append(e('summary',group+' · '+questions.length+' questions'));
    questions.forEach(q=>{
     const row=e('article',null,'bx-question-row'),label=e('label',q.question),input=e('textarea'),status=e('select');
     if(q.priority)row.append(e('span','Essential','bx-question-essential'));
     input.rows=2;input.maxLength=3000;input.value=answers[q.id]?.answer||'';input.placeholder=options.public?'Enter your answer or clarification…':'Record Blocktexx’s answer, the agreed assumption or a follow-up question…';input.setAttribute('aria-label',q.question+' — answer');label.append(input);
     [['open','Open'],['awaiting','Awaiting Blocktexx'],['answered','Answered'],['not_applicable','Not applicable']].forEach(([value,text])=>{const o=e('option',text);o.value=value;status.append(o);});status.value=answers[q.id]?.status||'open';status.setAttribute('aria-label',q.question+' — status');
     const change=()=>{if(options.public&&input.value.trim()&&['open','awaiting'].includes(status.value))status.value='answered';answers[q.id]={answer:input.value,status:status.value};onChange();count();};input.oninput=change;status.onchange=change;
     row.append(label,status);details.append(row);
    });list.append(details);
   });
  }
  draw();
 }
 return {bank,render};
})();
