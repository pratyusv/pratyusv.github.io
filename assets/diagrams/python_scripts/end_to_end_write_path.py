from diagrams import Cluster, Diagram, Edge
from diagrams.generic.blank import Blank

# 1. STYLE CONFIGURATION
graph_attr = {
    "fontsize": "14",
    "fontname": "Arial Bold",
    "bgcolor": "#ffffff",
    "splines": "orthogonal", 
    "nodesep": "0.6",
    "ranksep": "0.9",
}

# Base style for standard boxes (Memtable, Indexes)
base_style = {
    "fontname": "Arial",
    "fontsize": "11",
    "shape": "box",
    "style": "filled,rounded",
    "height": "0.8",
    "width": "1.6",
}

# Style for special shapes (WAL, Data Blocks)
# Removes 'shape' to allow manual overriding per-node
special_style = {k: v for k, v in base_style.items() if k != 'shape'}

with Diagram("SSTable & LSM-Tree Architecture", show=False, filename="sstable_bytebytego", direction="LR", graph_attr=graph_attr):
    
    client = Blank("Client Application", labelloc="c", 
                   **base_style, fillcolor="#e9ecef", color="#495057")

    # --- IN-MEMORY DOMAIN (RAM) ---
    with Cluster("1. IN-MEMORY (RAM)", graph_attr={"bgcolor": "#fffdeb", "color": "#f1c40f"}):
        memtable = Blank("Memtable\n(Sorted Skiplist)", **base_style, fillcolor="#fff9db", color="#fab005")

    # --- PERSISTENT STORAGE DOMAIN (DISK) ---
    with Cluster("2. PERSISTENT STORAGE (DISK)", graph_attr={"bgcolor": "#f8f9fa", "color": "#ced4da"}):
        wal = Blank("Write-Ahead Log\n(Append-Only)", **special_style, shape="note", fillcolor="#fff5f5", color="#ff8787")
        
        with Cluster("SSTable File Components", graph_attr={"bgcolor": "#ffffff", "color": "#495057", "style": "dashed"}):
            bloom = Blank("Bloom Filter\n(Fast Negative Check)", **base_style, fillcolor="#e3fafc", color="#15aabf")
            idx_summary = Blank("Index Summary\n(Sparsely Sampled)", **base_style, fillcolor="#e3fafc", color="#15aabf")
            primary_idx = Blank("Primary Index\n(Key-to-Offset Map)", **base_style, fillcolor="#edf2ff", color="#4c6ef5")
            data_block = Blank("Data Blocks\n(Compressed KVs)", **special_style, shape="cylinder", fillcolor="#f1f3f5", color="#343a40")

    compaction = Blank("Compaction Engine\n(Merge & Purge)", **base_style, fillcolor="#e6fcf5", color="#0ca678")

    # --- CONNECTIONS & SEQUENTIAL PATHS ---
    write_edge = {"color": "#fa5252", "fontcolor": "#fa5252", "style": "bold"}
    read_edge = {"color": "#40c057", "fontcolor": "#40c057", "style": "bold"}

    # The Write Path Sequence
    client >> Edge(label=" [1] Write", **write_edge) >> wal
    client >> Edge(label=" [2] Update", **write_edge) >> memtable
    memtable >> Edge(label=" [3] Flush", color="#fd7e14", fontcolor="#fd7e14", style="dashed") >> bloom

    # The Read Path Sequence
    client >> Edge(label=" [A] Lookup", **read_edge) >> bloom
    bloom >> Edge(label=" [B] Hit", **read_edge) >> idx_summary
    idx_summary >> Edge(label=" [C] Seek", **read_edge) >> primary_idx
    primary_idx >> Edge(label=" [D] Read", **read_edge) >> data_block
    data_block >> Edge(label=" [E] Return", **read_edge) >> client