'use strict';
function openSetupSection(){if(location.hash==='#materials'){const section=document.getElementById('materials');if(section)section.open=true;}}
openSetupSection();window.addEventListener('hashchange',openSetupSection);
