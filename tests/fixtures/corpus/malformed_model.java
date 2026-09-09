/*
 * Malformed model — edge cases for parser robustness.
 * Contains comments with API patterns, string literals with .create(),
 * incomplete method chains, and unusual formatting.
 */

import com.comsol.model.*;

public class malformed_model {

    public static void main(String[] args) {
        Model model = ModelUtil.create("Model");

        // This comment mentions .create("fake", "NotReal") but should be ignored
        // Also .set("commented_prop", "value") in a comment

        model.component().create("comp1", true);

        /* Multi-line comment with
         * .physics().create("phys1", "FakePhysics")
         * that should be skipped
         */

        // Valid geometry but with unusual whitespace
        model.component("comp1").geom().create("geom1", 3);
        model.component("comp1").geom("geom1").create(  "blk1"  ,  "Block"  );
        model.component("comp1").geom("geom1").feature("blk1").set("size",new double[]{1,1,1});

        // String that looks like an API call but isn't
        String desc = "This model uses .create() and .set() patterns";

        // Incomplete line — no semicolon (parser should still extract)
        model.component("comp1").geom("geom1").feature("blk1").set("pos", new double[]{0, 0, 0});

        model.component("comp1").geom("geom1").run("fin");

        // Empty physics block — no features created
        model.component("comp1").physics().create("empty", "EmptyPhysics", "geom1");
    }
}
