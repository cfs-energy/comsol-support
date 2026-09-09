/**
 * Minimal mutate contract example — returns the model unchanged.
 * Used by edit-mph contract-detection tests.
 */

import com.comsol.model.*;

import java.util.Map;

public class IdentityMutator {

    public static Model mutate(Model m, Map<String,String> args) {
        return m;
    }
}
