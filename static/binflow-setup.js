'use strict';
function openSetupSection(){if(['#materials','#bin-sizes'].includes(location.hash)){const section=document.getElementById(location.hash.slice(1));if(section)section.open=true;}}
openSetupSection();window.addEventListener('hashchange',openSetupSection);
