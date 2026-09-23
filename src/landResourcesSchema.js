export const LAND_SCOPES = [
  { id: "wua", label: "ՋՕԸ ամբողջ տարածքը", available: false },
  { id: "lower_hrazdan", label: "Ստորին Հրազդան I + II" },
  { id: "stage_1", label: "I հերթ" },
  { id: "stage_2", label: "II հերթ" },
];

export const LAND_MODES = [
  {
    id: "activity_2026",
    label: "2026 ակտիվություն",
    classes: [
      { id: "active", label: "Ակտիվ մշակվող", color: "#31c982" },
      { id: "partial", label: "Մասամբ ակտիվ", color: "#f0c64a" },
      {
        id: "no_current_activity",
        label: "Ընթացիկ ակտիվություն չի դիտվել",
        color: "#df3f4a",
      },
    ],
  },
  {
    id: "land_use_type",
    label: "Օգտագործման տեսակ",
    classes: [
      { id: "annual", label: "Միամյա մշակաբույսեր", color: "#e7bd46" },
      { id: "perennial", label: "Բազմամյա մշակաբույսեր", color: "#1fa276" },
      {
        id: "undetermined",
        label: "Տեսակը չի որոշվել",
        color: "#8d9691",
      },
      { id: "household", label: "Տնամերձ գյուղատնտեսություն", color: "#d66d9e" },
    ],
  },
  {
    id: "history_2021_2025",
    label: "2021–2025 պատմություն",
    classes: [
      { id: "stable_active", label: "Կայուն մշակվող", color: "#249b6b" },
      { id: "periodic", label: "Պարբերաբար մշակվող", color: "#e2aa43" },
      {
        id: "stable_no_activity",
        label: "Կայուն առանց դիտվող ակտիվության",
        color: "#8d9691",
      },
      {
        id: "insufficient",
        label: "Պահանջվում է լրացուցիչ ստուգում",
        color: "#9b5de5",
      },
    ],
  },
  {
    id: "potential",
    label: "Ներուժ",
    classes: [
      {
        id: "gravity_candidate",
        label: "Ինքնահոս ոռոգման նախնական թեկնածուներ",
        color: "#5b8ff9",
      },
      {
        id: "mechanical_candidate",
        label: "Մեխանիկական ոռոգման նախնական թեկնածուներ",
        color: "#d65c91",
      },
    ],
  },
];
