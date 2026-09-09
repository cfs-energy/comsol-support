# query-mph — read-only introspection of an existing `.mph`

The read-only sibling of `edit-mph`. Where `edit-mph` is built around
mutate → **save**, `query-mph` loads a model, hands it to a query class,
prints whatever that class returns as JSON, and **discards the model** —
no multi-minute, multi-GB write for what is purely inspection.

Use it to list a coupling-operator's selection, count mesh elements by
type, read geometry-entity coordinates, evaluate an expression on a
stored solution, or any other look-but-don't-touch question. What gets
inspected is entirely up to the query class; the command is model- and
topic-agnostic.

## The query contract

```java
public static java.util.Map<String,Object> query(
    com.comsol.model.Model m,
    java.util.Map<String,String> args)
```

The returned map is JSON-serialized into the result envelope and a
`query_result` telemetry event. The serializer handles `Map` (→ object),
`Collection`/`Iterable` and arrays (→ array, including primitive arrays),
the boxed primitives, `String`, and `null`; anything else falls back to
its quoted `toString()`. A non-`Map` return value is wrapped as
`{"value": …}`. Non-finite doubles (`NaN`/`Inf`) are emitted as quoted
strings (JSON has no literal for them).

Example — a topic-agnostic probe of global parameters and tags:

```java
import com.comsol.model.*;
import java.util.*;

public class InfoQuery {
    public static Map<String,Object> query(Model m, Map<String,String> args) {
        Map<String,Object> out = new LinkedHashMap<>();
        out.put("param_names", Arrays.asList(m.param().varnames()));
        out.put("component_tags", Arrays.asList(m.component().tags()));
        out.put("study_tags", Arrays.asList(m.study().tags()));
        return out;
    }
}
```

## CLI

```bash
comsol-support query-mph \
    --input  existing.mph \
    --query  InfoQuery.java \
    --arg    key=value           # repeatable; forwarded to query(...)
```

The query payload is printed to **stdout** as JSON; a one-line
provenance footer (query class, solved flag, event/halt) goes to stderr.

Options:

- `--solve STUDY_TAG` — run a study *before* querying (e.g. to inspect
  post-solve state). Still never saves.
- `--output PATH` — used **only** to name the telemetry/provenance
  sidecars; no `.mph` is written. Defaults to `<input>.query.mph`.
- `--license-timeout SECONDS` — bound the license checkout; fall back to
  `$COMSOL_LICENSE_TIMEOUT`. See [run-harness.md](run-harness.md).
- `--timeout`, `--workspace`, `--comsol-path`, `--no-sidecar`, `--stream`.

## Python API

`query-mph` is implemented on top of `edit_mph(query_java=…,
no_save=True)`, so the compile / classpath / telemetry / error-detail
plumbing is shared:

```python
from comsol_support.edit_mph import edit_mph
r = edit_mph(
    input_mph="existing.mph", output_mph="existing.query.mph",
    query_java="InfoQuery.java", no_save=True,
)
print(r.query_result)   # the deserialized Map<String,Object>
```

## Relationship to `edit-mph --no-save`

`edit-mph --no-save` is the same discard-the-model path without a query
class — useful to *solve* or *mutate-then-inspect-telemetry* on a large
model without paying the save cost. `query-mph` adds the structured
read-only return value. Both leave no `.mph` on disk and both still emit
the full telemetry stream (including `error_detail` on failure — see
[mphedit.md](mphedit.md)).

## Exit codes

`0` success · `1` runtime/Java error · `2` bad `--arg` · `3` query
contract not satisfied · `4` input `.mph` invalid.
