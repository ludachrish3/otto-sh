// otto.libsonnet: the scaffolding otto ships for lab templates.
//
//   local o = import 'otto.libsonnet';
//
// The import always resolves to this file, from otto's package data, whatever
// the import roots hold. A template may read it, wrap it or ignore it: nothing
// here is otto behaviour a template could not write itself, and it imports
// nothing.
//
// `version` is for authors only: a note for whoever reads this file or a
// template that checks it. otto never reads it. Render keys and fingerprints
// use this file's content hash, so any change to the file moves them, whether
// or not `version` moved.
//
// A template's `otto` argument holds `inventories` ({name: [record...]}, each
// record sorted by key) and `instances` ({name: {instance: {members, roles}}},
// or null where an inventory's adapter declares no instances). A record is
// {inventory, key, <public fields>...}: only the fields its inventory states
// are present, so the library reads every optional field with a default.
//
// Strings are joined with `+`, never `%`, on every per-record and
// per-element path: std.format is itself written in Jsonnet and costs about
// 0.3 ms a call under gojsonnet, which a fleet of thousands of hosts feels.

// Every ASCII character as escapeName writes it: those Python's re.escape
// escapes (the same set from Python 3.7 through 3.14) behind a backslash,
// the rest as they are; re.escape keeps every non-ASCII character. A table
// indexed by character, because std.objectHas costs a scan of the object's
// fields under gojsonnet and an index does not.
//
// A NUL is refused, not escaped: no name holds one. std.stringChars yields
// it as '\u0000' under gojsonnet and as '' under the C++ binding, so the
// table holds both as an error, a field Jsonnet evaluates only on a lookup,
// and the ASCII test compares strings rather than call std.codepoint, which
// fails on ''. A name without a NUL pays nothing for the refusal.
local regexSpecial = std.stringChars('()[]{}?*+-|^$\\.&~# \t\n\r\u000b\f');
local nulRefused = error 'escapeName: a name holds a NUL character, and names never contain one';
local asciiEscaped =
  { [std.char(c)]: std.char(c) for c in std.range(0, 127) }
  + { [c]: '\\' + c for c in regexSpecial }
  + { '\u0000': nulRefused, '': nulRefused };

// *s* as a regex that matches only *s* (see escapeName).
local escape(s) =
  std.join('', [
    if c < '\u0080' then asciiEscaped[c] else c
    for c in std.stringChars(s)
  ]);

// The element named *name*, with one host, *host* plus a qualified inventory
// reference to *record*, and *patterns* as its `labs` membership, unchanged.
local elementOf(name, record, host, patterns) = {
  name: name,
  labs: patterns,
  hosts: [host { inventory: { name: record.inventory, key: record.key } }],
};

// A record's qualified name, `<inventory>/<key>`, as reasons name it.
local qualified(record) = record.inventory + '/' + record.key;

// A record's place in the one record order, (inventory, key): NUL, below
// every other character, separates the parts, so the string order is the
// pair's for any inventory name without a NUL, and depends on nothing but
// the record for any name at all.
local recordOrder(record) = record.inventory + '\u0000' + record.key;

// *records* in (inventory, key) order, never their enumeration order.
local sortRecords(records) = std.sort(records, recordOrder);

// The first name *names* holds more than once, or null.
local firstDuplicate(names) =
  local sorted = std.sort(names);
  local repeated = [
    sorted[k]
    for k in std.range(1, std.length(sorted) - 1)
    if sorted[k] == sorted[k - 1]
  ];
  if std.length(repeated) == 0 then null else repeated[0];

// The value of *field* that a grouping scope reads from *record*: its fact
// (a top-level field) first, then its label of that name, else null.
local scopeValue(record, field) =
  local labels = std.get(record, 'labels', {});
  if std.objectHas(record, field) then record[field]
  else if std.objectHas(labels, field) then labels[field]
  else null;

// One record of a groupBy, with its scope values and the key of its instance:
// the values manifested as JSON (so 7 and '7' are different instances, and
// the key holds no raw NUL).
local scoped(record, scope) =
  local values = [scopeValue(record, f) for f in scope];
  { group: std.toString(values), values: values, record: record };

// The derived instance of *members*, scoped entries that share a group.
local derivedInstance(scope, members) =
  local fields = std.range(0, std.length(scope) - 1);
  local first = members[0];
  local problems = [
    qualified(m.record) + ' has no ' + scope[j]
    for m in members
    for j in fields
    if m.values[j] == null
  ];
  {
    id: { [scope[j]]: first.values[j] for j in fields },
    records: [m.record for m in members],
    roles: null,
    missing: [],
    'error': if std.length(problems) == 0 then null else problems[0],
  };

// A role's members in instance *i*: its roles' list when the instance has
// roles, else the records *role*'s `match` selects; in record order either way.
local roleMembers(i, name, role) =
  if i.roles != null then sortRecords(std.get(i.roles, name, []))
  else if std.objectHasAll(role, 'match') then
    sortRecords([r for r in i.records if role.match(r)])
  else error 'layout: role ' + name + ' has no match, and an instance without roles needs one';

// *value* as a name check's message spells what it got.
local described(value) =
  if value == null then 'null'
  else if std.isString(value) then 'an empty string'
  else 'a ' + std.type(value);

// *name*, which *what* returned for *where* in instance *i*, once it is a
// non-empty string. Anything else is an error naming the instance (its id
// manifested, and its error when it has one), never a silently dropped lab:
// a null field name drops its field, and Jsonnet cannot sort a null.
local checkedName(name, what, where, i) =
  if std.isString(name) && name != '' then name
  else error 'layout: ' + what + ' returned ' + described(name) + ' for ' + where
             + 'instance ' + std.manifestJsonMinified(i.id)
             + (if i['error'] != null then ' (' + i['error'] + ')' else '')
             + '; ' + what + ' must return a non-empty string, also for an instance'
             + ' whose id holds null';

// Why instance *i* cannot be used, given its members by role, or null.
local unavailableReason(i, roles, found) =
  local wrong = [
    name + ': expected ' + roles[name].count + ', found ' + std.length(found[name]) + ' ('
    + (if std.length(found[name]) == 0 then 'none'
       else std.join(', ', [qualified(r) for r in found[name]]))
    + ')'
    for name in std.objectFields(roles)
    if std.length(found[name]) != roles[name].count
  ];
  if i['error'] != null then i['error']
  else if std.length(i.missing) > 0 then
    'no record for ' + std.join(', ', [qualified(m) for m in i.missing])
  else if std.length(wrong) > 0 then std.join('; ', wrong)
  else null;

{
  // The library's version, for authors only (see the header).
  version: '1',

  // escapeName(s): *s* as a regex that matches only *s*, escaping exactly the
  // characters Python's re.escape escapes, so a generated lab name with
  // metacharacters can sit in an element's `labs` membership patterns. A name
  // that holds a NUL is an error, and so is one in the lab names `element`
  // and `layout` escape: theirs is this escape, and its error names escapeName.
  escapeName(s):: escape(s),

  // inGroup(name): a predicate, true for a record whose `groups` holds *name*.
  inGroup(name):: function(record) std.member(std.get(record, 'groups', []), name),

  // label(name, value): a predicate, true for a record whose label *name* is
  // *value* (of the same type: '7' is not 7).
  label(name, value)::
    function(record)
      local labels = std.get(record, 'labels', {});
      std.objectHas(labels, name) && labels[name] == value,

  // fact(name, value): a predicate, true for a record whose field *name*
  // (`site`, `rack`, `board`...) is stated and is *value*.
  fact(name, value):: function(record) std.objectHas(record, name) && record[name] == value,

  // inInventory(name): a predicate, true for a record from inventory *name*.
  inInventory(name):: function(record) record.inventory == name,

  // allOf(preds): a predicate, true when every predicate in *preds* is.
  allOf(preds):: function(record) std.all([p(record) for p in preds]),

  // anyOf(preds): a predicate, true when any predicate in *preds* is.
  anyOf(preds):: function(record) std.any([p(record) for p in preds]),

  // Both instance providers return a list of instances of one shape:
  //   {id: {<field>: value...}, records: [record...],
  //    roles: {role: [record...]} | null, missing: [{inventory, key}...],
  //    error: null | string}
  // `id` is the instance's identity, which lab and element names are built
  // from. `roles` is null when the provider assigns no roles, and a layout
  // then selects each role's members by its `match`. `missing` and `error`
  // make the instance's lab unavailable. Records are in (inventory, key) order.
  // `error` is a Jsonnet keyword, so a template reads it as i['error'].

  // instances(otto, inventory): the instances inventory *inventory*'s adapter
  // declares, sorted by name: `id` is {instance: <name>}, `roles` its roles
  // (null when it declares only members), and `missing` every member the
  // inventory has no record for, so an instance whose hosts are all gone is
  // still reported.
  instances(otto, inventory)::
    if !std.objectHas(otto.inventories, inventory) then
      error 'instances: the template declares no inventory ' + inventory
    else if otto.instances[inventory] == null then
      error 'instances: inventory ' + inventory
            + " declares no instances (its adapter has none); derive them with groupBy"
    else
      local declared = otto.instances[inventory];
      local names = std.objectFields(declared);
      local keysOf(stated) = std.set(
        stated.members
        + std.flattenArrays([stated.roles[r] for r in std.objectFields(stated.roles)])
      );
      // Every member key to its record, or to null when the inventory has
      // none: indexed, never probed with std.objectHas (see asciiEscaped).
      local recordOf =
        { [k]: null for k in std.set(std.flattenArrays([keysOf(declared[n]) for n in names])) }
        + { [r.key]: r for r in otto.inventories[inventory] };
      local present(keys) = [recordOf[k] for k in std.set(keys) if recordOf[k] != null];
      local one(name) =
        local stated = declared[name];
        local roleNames = std.objectFields(stated.roles);
        local keys = keysOf(stated);
        {
          id: { instance: name },
          records: present(keys),
          roles: if std.length(roleNames) == 0 then null
                 else { [r]: present(stated.roles[r]) for r in roleNames },
          missing: [{ inventory: inventory, key: k } for k in keys if recordOf[k] == null],
          'error': null,
        };
      [one(name) for name in names],

  // groupBy(records, scope): instances derived by grouping *records* on the
  // fields in *scope*, a list of names each read from a record's facts, then
  // its labels; records from several inventories may share an instance.
  // `id` maps each scope field to its value, so a scope must name every field
  // that makes an instance unique (['site', 'rack']). A record missing a
  // scope value sets `error` ('<inventory>/<key> has no <field>') on its own
  // instance only, whose `id` holds null for that field. Instances are in
  // the order of their scope values manifested as one JSON array (so
  // deterministic, and independent of the records' order, but not each
  // field's own order: sites 'a b', 'a!' and 'a' sort in that order).
  groupBy(records, scope)::
    local sorted = std.sort(
      [scoped(r, scope) for r in records],
      function(e) e.group + '\u0000' + recordOrder(e.record)
    );
    local n = std.length(sorted);
    // Sorted by instance key first, each instance's records are one run.
    local starts = [
      k
      for k in std.range(0, n - 1)
      if k == 0 || sorted[k].group != sorted[k - 1].group
    ];
    local ends = starts[1:] + [n];
    [
      derivedInstance(scope, sorted[starts[g]:ends[g]])
      for g in std.range(0, std.length(starts) - 1)
    ],

  // element(name, record, host, labs): an element named *name*, a member of
  // each lab in *labs* (escaped, so each pattern matches only that lab), with
  // one host: *host* plus a qualified inventory reference to *record*.
  element(name, record, host, labs)::
    elementOf(name, record, host, [escape(l) for l in labs]),

  // layout(spec): a lab document with one lab per instance, and its elements.
  //   spec = {
  //     instances: [instance...],               // from instances() or groupBy()
  //     lab(i): <lab name>,
  //     elementName(i, role, record, n): <element name>,
  //     roles: {<role>: {match: predicate, count: N,
  //                      host: {label: <string>, fn: function(record) host}}},
  //     labsEntry(i): <labs table entry>,      // optional, default {}
  //   }
  // A role's members are the instance's own for that role when it has roles,
  // else the records `match` selects, in (inventory, key) order; never
  // paired by enumeration order. Its `n`th member (from 0) becomes the
  // element `elementName(i, role, record, n)`. An instance with a member
  // count other than `count`, a missing member or an error emits no element,
  // and its lab entry is {unavailable: <reason>} rather than labsEntry(i).
  // lab(i) and elementName(...) must return non-empty strings, or the render
  // fails naming the instance. lab(i) is called for every instance, an
  // unavailable one included: an error instance's `id` holds null for the
  // scope value its record lacks, so a name built with `+` spells it 'null'
  // and the lab is unavailable, while a name that is that null, or calls a
  // std function on it, fails the whole render (Jsonnet has no try).
  // A lab or element name emitted twice is an error. The document's `_otto`
  // key, comment space to otto's loader, holds each element's provenance:
  // {provenance: {<element>: {instance: <id>, role: <role>, host: <label>}}}.
  layout(spec)::
    local roles = spec.roles;
    local labsEntry =
      if std.objectHasAll(spec, 'labsEntry') then spec.labsEntry else function(i) {};
    local plan(i) =
      local lab = checkedName(spec.lab(i), 'lab(i)', '', i);
      local patterns = [escape(lab)];
      local found = {
        [name]: roleMembers(i, name, roles[name])
        for name in std.objectFields(roles)
      };
      local why = unavailableReason(i, roles, found);
      {
        lab: lab,
        entry: if why == null then labsEntry(i) else { unavailable: why },
        emitted: if why != null then [] else [
          local record = found[name][n];
          local elementName = checkedName(
            spec.elementName(i, name, record, n),
            'elementName(i, role, record, n)',
            'role ' + name + ', record ' + qualified(record) + ', ',
            i
          );
          {
            name: elementName,
            element: elementOf(elementName, record, roles[name].host.fn(record), patterns),
            provenance: { instance: i.id, role: name, host: roles[name].host.label },
          }
          for name in std.objectFields(roles)
          for n in std.range(0, std.length(found[name]) - 1)
        ],
      };
    local plans = [plan(i) for i in spec.instances];
    local emitted = std.flattenArrays([p.emitted for p in plans]);
    local duplicateLab = firstDuplicate([p.lab for p in plans]);
    local duplicateElement = firstDuplicate([e.name for e in emitted]);
    if duplicateLab != null then
      error "layout emits the lab name '" + duplicateLab + "' more than once"
    else if duplicateElement != null then
      error "layout emits the element name '" + duplicateElement + "' more than once"
    else {
      labs: { [p.lab]: p.entry for p in plans },
      elements: [e.element for e in emitted],
      _otto: { provenance: { [e.name]: e.provenance for e in emitted } },
    },
}
