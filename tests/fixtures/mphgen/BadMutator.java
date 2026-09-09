/**
 * Negative-fixture mutator: missing the contract method. Used by the
 * edit-mph contract-detection test to assert ContractError is raised.
 */

import com.comsol.model.*;

public class BadMutator {

    public static Model notMutate(Model m) {
        return m;
    }
}
