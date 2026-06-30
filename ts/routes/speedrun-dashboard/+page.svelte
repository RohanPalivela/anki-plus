<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { PageData } from "./$types";
    import SpeedrunDashboard from "./SpeedrunDashboard.svelte";

    export let data: PageData;
</script>

<SpeedrunDashboard score={data.score} />
