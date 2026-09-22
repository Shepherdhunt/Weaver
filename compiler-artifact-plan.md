Add a **toolchain adapter and target-profile layer** to the pointer-migration project. It should collect evidence from the actual production compiler, optionally create a separate Clang analysis view, and normalize the results into a source-linked graph that an LLM can query.

This companion to the pointer-tracker plan covers artifact collection, compiler differences, and target fidelity. Public documentation was reviewed on September 22, 2026. Commercial compiler export switches remain unverified unless explicitly identified below; the recipes have not been executed against a user's installed SDK. A compiler product name is insufficient to select an adapter: record its release, build, executable identity, target, and options.

1. **Keep the artifact types and their roles distinct.**

   An AST represents source declarations, expressions, and resolved types. LLVM IR represents lowered computation and memory operations; `.ll` is its textual representation and `.bc` is its binary bitcode representation. GCC uses its own representations, including GENERIC, GIMPLE/SSA, and RTL. These are complementary views rather than interchangeable formats.

   | Artifact to collect | Contribution to pointer analysis | Important limit |
   |---|---|---|
   | Actual compile commands, response files, tool versions, configuration | Reconstructs what was compiled and under which assumptions | A compilation database alone omits much of linking and runtime configuration |
   | Preprocessed source, macro definitions, include dependencies and provenance | Exposes conditional code, hidden declarations, SDK types | Preprocessed text alone loses information needed to edit macro-based source safely |
   | AST with types, qualifiers, casts, source locations and macro mappings | Locates pointer declarations and safe source-edit positions | Parsing does not establish aliasing, lifetime, or whole-program behavior |
   | Control-flow graph, call graph, def-use and memory-flow summaries | Traces assignments, calls, mutations, and escapes | Indirect and external calls require conservative modeling |
   | LLVM IR/bitcode or GCC GIMPLE/SSA | Supports compiler-assisted flow and alias analysis | Target-specific and optimization-dependent; not a complete source model |
   | Type-layout report and ABI probes | Establishes sizes, alignments, offsets, and interface representations | Representative probes detect mismatches without proving all compiler semantics equivalent |
   | Native objects/executables and debug information | Cross-checks emitted types, layouts, symbols, and source mappings | Optimized or stripped output can omit information |
   | Link commands, scripts, map, archive members, symbols and relocations | Identifies image composition, special addresses, vectors, and section placement | Final linked images may no longer retain all object-level relocations |
   | Assembly/listings, disassembly, stack reports | Helps examine calling conventions, MMIO operations, and resource effects | Machine code cannot reconstruct the complete original AST |
   | OS/BSP/API contracts and runtime traces | Models borrowed buffers, callbacks, DMA, interrupts, and ownership | Observations cannot establish every possible target or execution |

   LLVM's opaque pointers use `ptr`, so the IR pointer type itself does not retain the C pointee type. Keep AST and source/debug mappings alongside IR. Also, compilers introduce IR pointers for ordinary local storage: IR pointer counts cannot certify pointer-free CLite source. [LLVM opaque pointers](https://llvm.org/docs/OpaquePointers.html)

2. **Implement the compiler capability matrix by version.**

   | Compiler or environment | Native collection path | LLVM/AST strategy |
   |---|---|---|
   | Upstream Clang/LLVM | Preprocessing, AST, LLVM IR/bitcode, dependency files, debug objects, assembly | Direct export; pin and probe frontend debug interfaces |
   | GCC, including embedded cross-GCC | Preprocessing, GENERIC/GIMPLE/SSA/alias/IPA/RTL dumps, debug objects, assembly | GCC dumps are native evidence. A separate Clang pass can supply LLVM/Clang AST evidence after compatibility checks |
   | Wind River LLVM in a VxWorks SDK | Use the installed SDK's compiler, target flags, headers, and runtime-mode settings | Vendor documentation identifies Clang/LLVM in the cited SDK; test export capabilities in the actual release |
   | Diab 7.0.x / 7.x LLVM family | Native compiler outputs and documented vendor capabilities | Wind River identifies LLVM/Clang technology; do not assume every upstream export flag is exposed |
   | Diab 5.x / 5.9.x family | Native preprocessor, listings, objects/debug data and link outputs where documented | Wind River identifies EDG in 5.x. No native LLVM export was verified in the public sources reviewed; use a separately labeled analysis frontend if compatible |
   | Green Hills optimizing compiler | Native preprocessing/listing options from installed manuals; documented ELF/DWARF output; linker artifacts | Public sources reviewed did not establish LLVM bitcode or Clang AST export. Use a version-specific native adapter and optional secondary analysis frontend |
   | LynxOS / LynxOS-178 SDK | Identify the actual compiler first; Lynx documents GCC/GNU toolchains | Use the GCC adapter when appropriate; do not infer a unique compiler from the OS name |

   Sources for native capabilities and family distinctions: [Clang driver](https://clang.llvm.org/docs/CommandGuide/clang.html), [GCC developer options](https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html), [VxWorks 7 SDK 22.03 example](https://labs.windriver.com/downloads/wrsdk-vxworks7-docs/2203/win/README_qemu.html), [Diab overview](https://www.windriver.com/resource/wind-river-diab-compiler-product-overview), [Green Hills open interfaces](https://www.ghs.com/OpenSystems.html), [Lynx Luminosity GNU tools](https://www.lynx.com/products/luminosity-eclipse-based-ide), [Lynx FAQ](https://www.lynx.com/resources-faq).

   VxWorks and LynxOS are operating systems. MULTI is an IDE/debugger and can support multiple compiler families. Capture the executable that actually compiled each file. The Lynx FAQ contains an older GCC version and a planned 2023 upgrade; it does not establish the version installed in a current project. [MULTI compiler support](https://www.ghs.com/products/MULTI_IDE.html)

   Mark capabilities `documented`, `probe-passed`, `unverified`, or `unavailable-in-this-profile`. A successful help-option lookup is insufficient: run a small fixture and verify that the artifact parses and contains expected pointer types, calls, layouts, and target metadata. A failed probe may reflect missing SDK configuration; distinguish that from an unsupported feature.

3. **Add a build-capture adapter before changing compiler flags.**

   Capture actual per-file argument arrays, working directories, response-file contents, compiler/assembler/linker identity, include search order, explicit and predefined macros, forced includes, relevant environment, generated headers, dependency hashes, libraries, and link inputs. Retain original source and preprocessor spelling/expansion locations. Record which source/configuration combinations have not been built.

   Prefer the build system's compilation database when available; supplement it with compiler/linker wrappers or build logs. Wrappers should call the original tool, preserve its exit status, and place analysis outputs in a separate directory. An incremental build may miss unchanged files, so establish inventory completeness from a clean configured build or a validated full command manifest. A compile database records translation-unit commands and permits multiple commands for one file. [Clang compilation database](https://clang.llvm.org/docs/JSONCompilationDatabase.html)

   Do not substitute a generic host compiler command. Even the same source file can have different semantics under another board, OS, feature set, library mode, or optimization configuration.

4. **Provide concrete Clang collection recipes.**

   The following are command templates. `unit.clang.rsp` must contain that translation unit's preserved or explicitly translated target, ABI, dialect, include, macro, optimization, and semantic options. Remove source filenames and conflicting output/action/dependency-output options into a logged invocation record; handle LTO and driver-specific options explicitly. Retain the original invocation separately. Execute from its recorded working directory and write outputs into an isolated collection directory. Paths shown here are placeholders.

   ```sh
   # Preprocessed code with source line markers.
   clang @unit.clang.rsp -E unit.c -o unit.i

   # Final macro definitions for this translation unit/configuration.
   clang @unit.clang.rsp -E -dM unit.c -o unit.macros.txt

   # Dependencies including system headers, rather than only project headers.
   clang @unit.clang.rsp -M -MF unit.d unit.c

   # Source AST; diagnostic output must also be retained separately.
   clang @unit.clang.rsp -fsyntax-only -Xclang -ast-dump=json unit.c > unit.ast.json

   # Human-readable LLVM IR, retaining the selected optimization settings.
   clang @unit.clang.rsp -g -fno-discard-value-names -S -emit-llvm unit.c -o unit.ll

   # Binary representation of LLVM IR for LLVM analysis tools.
   clang @unit.clang.rsp -g -fno-discard-value-names -c -emit-llvm unit.c -o unit.bc

   # Optional record-layout diagnostics; test this frontend interface first.
   clang @unit.clang.rsp -fsyntax-only -Xclang -fdump-record-layouts-complete unit.c > unit.layouts.txt
   ```

   The AST and layout switches are frontend inspection interfaces; their schemas and output behavior must be versioned and probed. For a durable tool, prefer a LibTooling exporter that writes the project's own versioned JSON schema over relying indefinitely on debug-dump formatting. Preserve raw dumps for audit. AST-internal IDs are not persistent source identities. [Clang AST tooling](https://clang.llvm.org/docs/LibASTMatchers.html), [upstream Clang 18 option definitions](https://github.com/llvm/llvm-project/blob/release/18.x/clang/include/clang/Driver/Options.td)

   Optional `-emit-ast` produces a serialized Clang AST for compiler tooling and caching; it is version-dependent and not the primary interchange format for the LLM. A separate `-E -dD` capture can retain definitions alongside preprocessed code. The final `-dM` macro list is not a complete record of expansion history. Keep compiler preprocessor callbacks or equivalent provenance when implementing the source rewriter. GCC and Clang dependency captures should include system/SDK headers; `-MM` and `-MMD` intentionally omit system-header dependencies. [Clang driver](https://clang.llvm.org/docs/CommandGuide/clang.html), [preprocessor options](https://gcc.gnu.org/onlinedocs/gcc/Preprocessor-Options.html)

   Where supported, another useful view adds `-Xclang -disable-llvm-passes` to the textual IR command. This requests frontend-generated IR before LLVM optimization passes while retaining the original optimization options seen by preprocessing and the frontend. It is an internal interface requiring a pinned/probed version, and its output is still lowered IR, not the original C AST.

   Do not blindly replace production `-O2` with `-O0`: optimization settings can change predefined macros and active header/source branches. An `-O0` view is a separate configuration unless preprocessing fidelity has been established. Keep the production-option view as well. Debug-info options added for collection should likewise be recorded as deviations. [Clang driver options](https://clang.llvm.org/docs/CommandGuide/clang.html)

   Keep producer and consumer LLVM versions compatible, especially for vendor forks. Retain the module target triple and data layout. Do not link analysis modules from different targets/ABIs or present a linked subset as the whole program. Libraries and assembly missing from IR require explicit boundary summaries.

5. **Provide GCC collection recipes without pretending they are LLVM.**

   `target-gcc` stands for the actual SDK cross-compiler executable. `unit.gcc.rsp` follows the same preservation rules as above, using GCC's own target and semantic flags. These examples use documented GCC dump names; availability and whether a pass executes depend on the installed version and optimization settings.

   ```sh
   target-gcc @unit.gcc.rsp -E unit.c -o unit.i
   target-gcc @unit.gcc.rsp -E -dM unit.c -o unit.macros.txt

   # Probe the passes in the selected configuration.
   target-gcc @unit.gcc.rsp -c -fdump-passes unit.c -o unit.probe.o

   # Native source-level, flow, alias, callgraph, and RTL views.
   target-gcc @unit.gcc.rsp -g -c unit.c -o unit.analysis.o \
     -fdump-tree-original -fdump-tree-gimple \
     -fdump-tree-cfg -fdump-tree-ssa -fdump-tree-alias \
     -fdump-ipa-cgraph -fdump-rtl-expand

   # Supplementary stack and assembly evidence where supported.
   target-gcc @unit.gcc.rsp -g -fstack-usage -c unit.c -o unit.stack.o
   target-gcc @unit.gcc.rsp -g -S unit.c -o unit.s
   ```

   Capture stderr from the pass probe and all emitted dump files; do not hard-code pass-number filename suffixes. A missing alias dump means the pass/artifact was unavailable or did not execute, not that the program has no aliases. Avoid all-pass dumps by default because of volume. GCC plugins are an optional later route to structured exports, but require a compatible compiler build/plugin interface and version-specific maintenance. [GCC developer options](https://gcc.gnu.org/onlinedocs/gcc/Developer-Options.html)

   Where supported, `-fcallgraph-info=su,da` adds callgraph output with stack-usage and dynamic-allocation information. LTO can change where these artifacts appear. Select a debug format/version compatible with the actual SDK rather than forcing the newest DWARF version onto older tools. [GCC debug options](https://gcc.gnu.org/onlinedocs/gcc/Debugging-Options.html)

   GCC `-flto` stores GCC's GIMPLE representation in object-file sections. It is not LLVM bitcode, and GCC documents strict version compatibility for its LTO representation. Do not add `-flto` merely to obtain `.bc` files or pass GCC LTO objects to LLVM as equivalent input. [GCC LTO documentation](https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html)

   If LLVM analyses are desired, either add a secondary Clang pass after target/dialect checks, or implement a GCC-native exporter to the common graph. A general GIMPLE-to-LLVM translator would itself be a substantial compiler project and should not be the first milestone.

6. **Handle Diab, Green Hills, and other vendor toolchains through capability adapters.**

   Use the installed version's manuals and small compile probes to determine support for preprocessing with line markers, macro/include reporting, dependencies, listings/assembly, debug information, linker maps, stack reports, and any supported AST/IR or compiler-extension APIs. Capture object/debug formats explicitly; do not assume every embedded profile emits ELF/DWARF.

   For Diab 7.x, investigate vendor-supported LLVM and frontend export modes first. Its LLVM ancestry is evidence about implementation, not proof that `-emit-llvm`, `-Xclang`, or upstream plugins are usable through the installed driver. Preserve vendor intrinsics and the embedded LLVM revision if exports work.

   For Diab 5.x and Green Hills, collect native artifacts even when a Clang analysis pass is available. The public vendor material reviewed did not supply a reliable current command reference for all requested exports, so this plan intentionally does not invent a common set of flags. An undocumented capability remains unverified rather than categorically unsupported.

   A secondary Clang frontend needs an explicit translation of each relevant compiler option and language extension. Prefer parsing original source with faithful headers and macro provenance. Production-preprocessed `.i` files can help select the same branches, but may still contain vendor syntax, builtins, or pragmas and are insufficient alone for precise edits to macro-based source. Record every substitution and every unsupported construct.

   If the target has no adequate Clang representation, retain native analysis and mark LLVM export unavailable for that profile. Do not analyze it as a nearby architecture and authorize changes as though it were the real target.

7. **Collect linked-image evidence independently of AST/IR.**

   Keep unstripped objects, archives and final images, native linker scripts/control files, maps and symbol reports. Where the target emits supported ELF/DWARF, useful examples are:

   ```sh
   llvm-readelf --file-header --sections --symbols --relocations firmware.elf > firmware.elf.txt
   llvm-dwarfdump --debug-info --debug-line firmware.elf > firmware.dwarf.txt
   llvm-objdump --disassemble --source firmware.elf > firmware.disassembly.txt
   ```

   Use compatible target-aware tools and native vendor utilities for unsupported formats/instructions. Read object files as well as the final image because linking can resolve relocations and discard unused sections or debug data. LLVM's DWARF tool inspects debug information; it is not an AST recovery tool. [LLVM DWARF tooling](https://llvm.org/docs/CommandGuide/llvm-dwarfdump.html), [LLVM object reader](https://llvm.org/docs/CommandGuide/llvm-readelf.html), [LLVM disassembler](https://llvm.org/docs/CommandGuide/llvm-objdump.html)

   For a GCC/Clang driver that actually invokes GNU ld, the existing link command can add `-Wl,-Map=firmware.map,--cref` to request a map and cross-reference table. This is linker-specific; use documented vendor equivalents elsewhere. Link scripts define placement and can expose address-dependent objects that C-only analysis misses. [GNU ld options](https://sourceware.org/binutils/docs/ld/Options.html), [linker scripts](https://sourceware.org/binutils/docs/ld/Scripts.html)

8. **Model architectures, operating systems, and compilers separately.**

   Create one profile for each supported combination of board/CPU, architecture features, ABI, compiler build, SDK/BSP, OS version, runtime mode, and build configuration. “ARM” or “PowerPC” alone is not sufficient. LLVM IR also contains target-specific layout information; it does not make these distinctions disappear. [LLVM data layout](https://llvm.org/docs/LangRef.html#data-layout)

   | Profile dimension | Facts to preserve |
   |---|---|
   | Architecture and CPU | ISA features, endian, address spaces, data/function pointer representations, unaligned-access constraints |
   | ABI and types | `CHAR_BIT`, integer and enum representation, floating-point ABI, pointer sizes/alignments, record packing, bit-fields, calling conventions |
   | Compiler semantics | C dialect/extensions, builtins, aliasing and overflow options, signed-char setting, volatile/atomic semantics, optimization and LTO |
   | SDK and BSP | Exact headers/libraries, forced includes, generated settings, startup and vector code, linker memory map |
   | OS and runtime | Bare-metal/kernel/process/partition context, tasks/ISRs, callbacks, allocation, ownership and synchronization contracts |
   | Hardware interaction | MMIO widths/order/side effects, DMA address/buffer requirements, cache maintenance, barriers, device-visible memory |

   For Clang cross analysis, use the SDK-appropriate `--target`, CPU/ABI/feature options, sysroot and include/library paths. Some target configurations need explicit paths beyond a sysroot. Do not let the host's default triple or headers supply the target model. [Clang cross-compilation](https://clang.llvm.org/docs/CrossCompilation.html)

   In VxWorks, distinguish a downloadable kernel module from a real-time process; their runtime contexts differ. Other systems may distinguish bare-metal images, kernel components, processes, or partitions. Capture the actual mode rather than inferring it from an OS name. [VxWorks SDK application guide](https://www.labs.windriver.com/downloads/wrsdk-vxworks7-docs/VxWorksSDK-ApplicationDeveloperGuide.html)

9. **Check secondary-analysis fidelity before allowing a rewrite.**

   Compare active declarations, constant values, included definitions, and semantic branches. Build representative layout/ABI probes with both compilers: sizes, alignments and member offsets; packed and bit-field structures; function/data-pointer distinctions; aggregate argument/return conventions; and relevant interrupt and atomic interfaces. Extract constants/layouts from target objects or debug data when possible; use target/emulator execution for properties requiring it. Passing representative probes does not establish unrestricted compiler equivalence.

   Preserve explicit evidence status per translation unit and affected region: `native`, `secondary-checked`, `secondary-partial`, or `unsupported`. Keep compatibility findings separate from transformation verification results. A successful parse or matching data layout alone never upgrades a pointer transformation to proven correct.

   Unknown inline assembly, MMIO, DMA effects, binary-only libraries, weak/overridden symbols, dynamic module loading, callback registration, and OS-owned storage require conservative summaries or a blocker. An external callback may have no visible C caller. Volatile access must retain the required access behavior, but volatile alone does not provide a synchronization barrier for ordinary memory. [GCC volatile documentation](https://gcc.gnu.org/onlinedocs/gcc/Volatiles.html)

   Do not erase unsupported attributes/pragmas, replace device access with no-op stubs, or assume unknown calls do not touch memory. Rewrite acceptance still requires the production toolchain and every claimed target profile, followed by relevant behavior, interface, resource and target tests from the main migration plan.

10. **Normalize evidence into a graph for the LLM.**

    Export source symbols/types/objects, pointer-valued expressions, memory regions, functions, call sites, and profile IDs. Add edges for assignment, possible targets, address-taking, loads/stores, field/index access, arguments/returns, allocation/release, escape, and callbacks. Record byte ranges/offsets, nullability, lifetime, read/write effects, address spaces, and external contracts when established. Keep unresolved targets and memory effects explicit.

    Each fact needs provenance: source location and hash, configuration, producing compiler/tool version, artifact location, assumptions, and evidence status. LLVM alias analysis and MemorySSA can support this graph, but they do not by themselves provide a complete cross-language, interprocedural or hardware model. MemorySSA is intraprocedural; alias analysis can report `MayAlias`. [LLVM alias analysis](https://llvm.org/docs/AliasAnalysis.html), [MemorySSA](https://llvm.org/docs/MemorySSA.html)

    Give the LLM a focused slice for a selected pointer: its declarations and source spans, possible targets, uses, callers, lifetimes, boundary effects, relevant native layout evidence, candidate recipes, and unresolved preconditions. Let it request more graph evidence and explain a bounded change. Avoid asking it to infer safety from a large raw IR dump.

    A minimal profile manifest can look like this; all values are placeholders:

    ```yaml
    profile_id: boardA-os-release
    source_revision: recorded_commit
    production:
      compiler: {family: recorded_family, version: recorded_version, hash: recorded_hash}
      assembler_linker_manifest: tools.json
      commands_manifest: commands.json
      sdk_bsp_manifest: sdk-inputs.json
    target:
      architecture: recorded_architecture
      cpu_features: []
      endian: recorded_endian
      abi: recorded_abi
      address_spaces: []
    platform:
      os_version: recorded_os_version
      runtime_mode: recorded_kernel_process_partition_or_bare_metal
      startup_and_link_inputs: link-inputs.json
    analysis:
      producer: native_or_secondary_frontend
      tool_versions: analysis-tools.json
      flag_translation_log: translations.json
      preprocessing_findings: preprocessing.json
      abi_findings: abi.json
      unresolved_constructs: unresolved.json
    artifacts:
      inventory: artifacts.json
      normalized_graph: graph.json
      validation_results: validation.json
    ```

    Cache by source/generated-input hashes, per-file command, tool identities and profile. Invalidate affected evidence whenever any of those changes.

11. **Add this to the implementation roadmap.**

    First, capture builds and native outputs for one GCC profile and one Clang profile. Produce a source-linked pointer inventory with configuration coverage and honest unknowns. Then add LLVM/GIMPLE flow analysis and cross-frontend fidelity checks. Finally add version-specific Diab, Green Hills, and other SDK adapters as those environments become available.

    For cFS, start with one pinned native development configuration and its actual compiler. Keep OSAL and PSP boundaries explicit. Add an embedded configuration as a separate profile and verify the same migration recipe there before claiming portability. Implement the common artifact schema before expanding the number of vendor integrations.

    Extend the migration LLM instruction with: “For every pointer finding and proposed rewrite, identify the production compiler and target profile, artifact provenance, secondary-frontend compatibility status, and unresolved external effects. An absent artifact or unsupported construct is unknown evidence. Never convert it into a no-alias or no-pointer conclusion.”

12. **Evaluate SVF as the first LLVM pointer-analysis backend.**

    SVF is a strong candidate for the analysis work between artifact collection and migration planning. It provides points-to analyses, call and value-flow graphs, and selectable analysis precision. Use it to help find the connected set of pointer uses that must change together. Retain the source rewriter, target checks, transformation recipes, and equivalence validation as separate components. [SVF features](https://github.com/SVF-tools/SVF#features-and-publications), [SVF design](https://github.com/SVF-tools/SVF/wiki/SVF-Design)

    The proposed path is: target-correct LLVM IR and explicit external models enter SVF; an adapter exports source-linked evidence; the migration rules and LLM combine that evidence with AST/native layout facts; the source rewriter produces a candidate patch; the production toolchain and validation gates evaluate it. SVF runs on the development/analysis host and need not be incorporated into the controller's firmware.

    First use a pinned SVF/LLVM combination as a separate analysis job for reproducibility, resource control, and failure isolation. Retain its input bitcode, external model versions, options, diagnostics, and completion status. The project also supplies an example of using SVF as a library, which is a possible later integration route. [SVF library example](https://github.com/SVF-tools/SVF-example)

    A documented starting command is:

    ```sh
    wpa -ander -svfg -print-pts -dump-callgraph -dump-vfg program.bc
    ```

    Probe this against the pinned version. Use Andersen-style analysis as the initial survey, then evaluate supported flow-sensitive or demand-driven/context-sensitive analyses for candidates needing more precision. These modes have different assumptions and costs; do not label every run as having every sensitivity. CLI text and DOT output are useful for inspection, but the durable integration should export the project's own versioned evidence schema. Do not assume a generic built-in JSON exporter works without testing it. [SVF user guide](https://github.com/SVF-tools/SVF/wiki/User-Guide), [current option definitions](https://github.com/SVF-tools/SVF/blob/master/svf/lib/Util/Options.cpp)

    Interpret targets as abstract objects under the selected memory model. An allocation site can represent multiple runtime allocations. A singleton points-to set therefore does not establish one runtime object, unique ownership, non-nullness, valid lifetime, or safe scalar replacement. Overlapping sets indicate possible sharing, not necessarily sharing on every execution. Disjointness is useful only within a sufficiently modeled and completed analysis. [SVF memory abstraction](https://github.com/SVF-tools/SVF/wiki/Technical-documentation#11-abstract-memory-objects)

    Supply actual code or reviewed pointer-effect models for external services, custom allocators, cFS/OSAL functions, and SDK APIs. SVF includes an external-API modeling mechanism, but those summaries do not replace temporal API contracts. For example, a cFS receive-buffer pointer needs the known success/read-only/next-receive lifetime contract in addition to its possible storage targets. Explicitly model task entrypoints, callbacks, and ISR inputs; do not assume a framework automatically provides arbitrary external inputs or RTOS scheduling behavior. [SVF external API modeling](https://github.com/SVF-tools/SVF/wiki/Handling-External-APIs-with-extapi.c)

    Propagate incomplete-run diagnostics, unknown calls, and resource/edge limits into the candidate record. For instance, current SVF code has an indirect-call resolution limit. An incomplete graph must never authorize a rewrite through an apparent absence of aliases or callees. [SVF call resolution](https://github.com/SVF-tools/SVF/blob/master/svf/lib/MemoryModel/PointerAnalysis.cpp)

    For the proof of concept, use small fixtures for direct aliases, conditional targets, aliased parameters, repeated allocation, function pointers, and an externally borrowed buffer. Measure source-mapping coverage, missing API models, precision, runtime/memory, and whether required blockers remain visible. Then analyze one cFS application plus its relevant dependencies and models. The first useful outcome is a source-selected pointer with explained possible targets, cross-function uses, and migration eligibility—not a whole-program rewrite.

    The current repository states AGPL-3.0-or-later licensing. Include the license of the pinned revision in dependency selection and review how it fits the intended distribution or hosted product. Running the analyzer separately is an engineering choice, not an automatic resolution of licensing requirements. [SVF license](https://github.com/SVF-tools/SVF/blob/master/LICENSE.TXT)
